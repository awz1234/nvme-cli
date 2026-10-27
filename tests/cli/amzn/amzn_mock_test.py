#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of nvme-cli.
"""Shared harness for the hardware-free amzn plugin tests.

libmock_nvme.c forwards every admin command to AmznController here, which
answers Identify Controller and the Amazon statistics log page (0xD0).

The plugin picks its code paths from the device: the magic of the log page
says whether the drive is an EBS volume or instance storage, the model name
in Identify Controller selects how much of the log is read, and the counts
in the log drive the histogram loops. All of that is set per test, so the
branches no single drive could reach are all reachable.

The layouts below are those of `struct amzn_latency_log_page` in
plugins/amzn/amzn-nvme.c; the offsets were taken with offsetof().

This module is imported by the amzn_*_mock_test.py suites; it registers no
tests of its own.
"""
import json
import signal
import struct
import sys
import time

from tests.cli.nvme_mock import (CNS_CTRL, ID_CTRL_SIZE, MockController,
                                 MockTestCase, id_ctrl, pack_fields)
from tests.cli.nvme_mock_ipc import popen_nvme

# The command which polls, for poll_stats().
_STATS = 'stats'

# A poll takes a second, and the mock answers it: enough for a slow or an
# emulated machine, short enough not to sit in a test timeout.
POLL_TIMEOUT = 60

# The statistics log page, and the magic which says what the drive is.
LID_STATS = 0xD0
EBS_MAGIC = 0x3C23B510
LOCAL_STORAGE_MAGIC = 0xEC2C0D7E

# The version of the log page which has the per-IO-size histograms.
DETAIL_IO_VERSION = 1

# The model name prefix which selects the instance storage path.
LOCAL_STORAGE_MODEL = 'Amazon EC2 NVMe Instance Storage'
EBS_MODEL = 'Amazon Elastic Block Store'

# sizeof(struct amzn_latency_log_page_base) and of the whole page: an EBS
# volume returns the base, instance storage the base and the detail IO.
BASE_SIZE = 4096
PAGE_SIZE = 8192

# struct amzn_latency_log_page_base
BASE_FIELDS = {
    'magic': ('<I', 0),
    'version': ('<I', 4),
    'total_read_ops': ('<Q', 8),
    'total_write_ops': ('<Q', 16),
    'total_read_bytes': ('<Q', 24),
    'total_write_bytes': ('<Q', 32),
    'total_read_time': ('<Q', 40),
    'total_write_time': ('<Q', 48),
    'ebs_volume_performance_exceeded_iops': ('<Q', 56),
    'ebs_volume_performance_exceeded_tp': ('<Q', 64),
    'ec2_instance_performance_exceeded_iops': ('<Q', 72),
    'ec2_instance_performance_exceeded_tp': ('<Q', 80),
    'volume_queue_length': ('<Q', 88),
    'num_of_hists': ('<I', 3600),
}

# struct amzn_latency_histogram: a u64 count of bins, then 64 bins of
# (u64 lower, u64 upper, u32 count, u32 reserved).
READ_HIST_OFFSET = 512
WRITE_HIST_OFFSET = 2056
HIST_BINS = 64
BIN_SIZE = 24

# __u32 hist_io_sizes[8], the upper bound of each per-IO-size histogram.
IO_SIZES_OFFSET = 3604
NUM_HISTOGRAM = 8

# struct amzn_latency_detail_io_histogram: per IO size, 32 read counts and
# 32 write counts, as u64.
DETAIL_IO_OFFSET = 4096
IO_HIST_SIZE = 512
DETAIL_BINS = 32

# The vendor specific area of Identify Controller, where the amzn id-ctrl
# command reads the block device name from.
VS_OFFSET = 3072
BDEV_SIZE = 32

# A histogram a drive could return: the bins of the first latency decade.
DEFAULT_BINS = (
    (0, 64, 7),
    (64, 128, 1024),
    (128, 256, 4096),
    (256, 512, 255),
    (512, 1024, 33),
)

# The upper bound of each per-IO-size histogram, as a drive reports them:
# 4 KiB to 512 KiB, and everything above.
DEFAULT_IO_SIZES = (4096, 8192, 16384, 32768, 65536, 131072, 262144,
                    0xffffffff)


def pack_id_ctrl(model=EBS_MODEL, bdev=None, **values):
    """An Identify Controller data structure of an Amazon drive. @bdev is
    the block device name in the vendor specific area, space padded as the
    drive does, and not terminated."""
    buf = id_ctrl(mn=model[:40], **values)
    if bdev is not None:
        name = bdev.encode('ascii')[:BDEV_SIZE]
        buf[VS_OFFSET:VS_OFFSET + BDEV_SIZE] = name.ljust(BDEV_SIZE, b' ')
    assert len(buf) == ID_CTRL_SIZE
    return bytes(buf)


def pack_histogram(bins, num_bins=None):
    """One struct amzn_latency_histogram. @bins is (lower, upper, count)
    for each bin; @num_bins overrides the count the drive reports, for the
    tests of what the plugin does with a count it cannot honour."""
    buf = bytearray(8 + HIST_BINS * BIN_SIZE)
    struct.pack_into('<Q', buf, 0,
                     len(bins) if num_bins is None else num_bins)
    for index, (lower, upper, count) in enumerate(bins):
        struct.pack_into('<QQII', buf, 8 + index * BIN_SIZE,
                         lower, upper, count, 0)
    return buf


def pack_stats_log(magic=EBS_MAGIC, version=DETAIL_IO_VERSION,
                   read_bins=DEFAULT_BINS, write_bins=DEFAULT_BINS,
                   num_read_bins=None, num_write_bins=None,
                   io_sizes=DEFAULT_IO_SIZES, num_of_hists=None,
                   detail=None, **values):
    """The Amazon statistics log page, see BASE_FIELDS for @values.

    @detail is {histogram index: (read counts, write counts)} of the
    per-IO-size histograms; it is only read from a page the plugin asks the
    whole of, i.e. from instance storage."""
    if num_of_hists is None:
        num_of_hists = len(io_sizes)
    buf = bytearray(pack_fields(BASE_FIELDS, PAGE_SIZE, magic=magic,
                                version=version, num_of_hists=num_of_hists,
                                **values))

    read = pack_histogram(read_bins, num_read_bins)
    buf[READ_HIST_OFFSET:READ_HIST_OFFSET + len(read)] = read
    write = pack_histogram(write_bins, num_write_bins)
    buf[WRITE_HIST_OFFSET:WRITE_HIST_OFFSET + len(write)] = write

    for index, size in enumerate(io_sizes[:NUM_HISTOGRAM]):
        struct.pack_into('<I', buf, IO_SIZES_OFFSET + 4 * index, size)

    for index, (reads, writes) in dict(detail or {}).items():
        start = DETAIL_IO_OFFSET + index * IO_HIST_SIZE
        for bin_index, count in enumerate(reads):
            struct.pack_into('<Q', buf, start + 8 * bin_index, count)
        for bin_index, count in enumerate(writes):
            struct.pack_into('<Q', buf,
                             start + 8 * (DETAIL_BINS + bin_index), count)
    return bytes(buf)


def detail_counts(first=1, step=1):
    """Counts for one per-IO-size histogram: every bin non-zero, and each
    one different, so that a wrong offset shows."""
    return [first + step * index for index in range(DETAIL_BINS)]


class StatsSequence:
    """Answers each read of the statistics log page from the next page of
    a sequence, the last one repeating.

    `nvme amzn stats --interval` polls the log and prints what changed
    since the previous poll, so the counters have to grow between reads.
    A page larger than the log page PDU size is read in several commands,
    so the page is picked when a read starts (at offset 0) and held for
    the rest of it.

    An entry of the sequence is a page, or an NVMe status for a poll the
    drive refuses."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.reads = 0
        self.index = 0

    def __call__(self, cmd):
        if cmd.lpo == 0:
            self.index = min(self.reads, len(self.pages) - 1)
            self.reads += 1
        page = self.pages[self.index]
        return page if isinstance(page, int) else page[cmd.lpo:]


class FailAfter:
    """Answers the first command with @data and every later one with
    @status: a drive which stops answering."""

    def __init__(self, data, status):
        self.data = data
        self.status = status
        self.answers = 0

    def __call__(self, cmd):
        self.answers += 1
        return self.data if self.answers == 1 else self.status


class AmznController(MockController):
    """Answers Identify Controller and the statistics log page. A test
    replaces either to pick the drive and the data it returns."""

    def __init__(self, sock_path):
        super().__init__(sock_path)
        self.identify[CNS_CTRL] = pack_id_ctrl()
        self.logs[LID_STATS] = pack_stats_log()


class AmznMockTestBase(MockTestCase):
    """Runs `nvme amzn ...` against a mocked Amazon drive."""

    controller_class = AmznController

    def setUp(self):
        super().setUp()
        # Set by set_stats_sequence(), for completed_polls().
        self.stats_sequence = None

    def set_drive(self, model=EBS_MODEL, bdev=None, **values):
        """Replace the Identify Controller data of the mocked drive."""
        self.server.identify[CNS_CTRL] = pack_id_ctrl(model=model,
                                                      bdev=bdev, **values)

    def set_stats(self, **kwargs):
        """Replace the statistics log page, see pack_stats_log()."""
        self.server.logs[LID_STATS] = pack_stats_log(**kwargs)

    def run_amzn(self, command, *args, device=None):
        return self.nvme('amzn', command,
                         self.ctrl if device is None else device, *args)

    def run_amzn_ok(self, command, *args, device=None):
        return self.assertOk(self.run_amzn(command, *args, device=device))

    def amzn_json(self, command, *args, device=None):
        """Run a command with JSON output and return the parsed object."""
        result = self.run_amzn_ok(command, '-o', 'json', *args,
                                  device=device)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f'nvme amzn {command} -o json wrote no valid JSON '
                      f'({exc}): {result.stdout!r}')

    def stats_log_reads(self):
        """The Get Log Page commands the run sent for the statistics log."""
        return self.server.log_reads(LID_STATS)

    def set_stats_sequence(self, pages):
        """Answer the polls of the log page from @pages, see
        StatsSequence."""
        self.stats_sequence = StatsSequence(pages)
        self.server.logs[LID_STATS] = self.stats_sequence
        return self.stats_sequence

    def completed_polls(self):
        """How often the command has read the log page. A page larger than
        the log page PDU size takes several commands, so count the reads
        which started one rather than the commands."""
        if self.stats_sequence is not None:
            return self.stats_sequence.reads
        return len(self.stats_log_reads())

    def poll_stats(self, *args, polls=1, timeout=POLL_TIMEOUT):
        """Run `nvme amzn stats --interval 1 *args`, let it poll the log
        @polls times after its first report, stop it with SIGINT as Ctrl+C
        would, and return (output, errors).

        The run is driven by what the mock has been asked for, not by a
        wall clock: a poll is a read of the log page. Its output is read
        once it has exited, because it writes a pipe in blocks and a
        report is smaller than one.

        The signal handler only sets a flag, so a report the command has
        started comes out whole."""
        proc = popen_nvme(self.nvme_bin, self.env, self.sysfs_dir,
                          self.base_dir, 'amzn', _STATS, self.ctrl,
                          '--interval', '1', *args)
        self.addCleanup(self._reap, proc)

        deadline = time.monotonic() + timeout
        while self.completed_polls() <= polls:
            if proc.poll() is not None:
                out, errors = proc.communicate()
                self.fail(f'nvme amzn stats stopped on its own with '
                          f'{proc.returncode}: {out!r} {errors!r}')
            if time.monotonic() > deadline:
                self._reap(proc)
                self.fail(f'nvme amzn stats read the log '
                          f'{self.completed_polls()} times in {timeout}s, '
                          f'expected {polls + 1}')
            time.sleep(0.05)

        proc.send_signal(signal.SIGINT)
        out, errors = proc.communicate(timeout=timeout)
        print(f'\n--- POLLED: nvme amzn stats --interval 1 '
              f'{" ".join(args)} ---\nSTDOUT:\n{out}\nSTDERR:\n{errors}'
              '\n-----------------------------------', file=sys.stderr)
        self.assertEqual(proc.returncode, 0,
                         f'nvme amzn stats failed: {errors!r}')
        return out, errors

    def poll_stats_until_exit(self, *args, timeout=POLL_TIMEOUT):
        """Run the polling command and wait for it to stop by itself, for
        the failures which end a run. Returns the finished process."""
        proc = popen_nvme(self.nvme_bin, self.env, self.sysfs_dir,
                          self.base_dir, 'amzn', _STATS, self.ctrl,
                          '--interval', '1', *args)
        self.addCleanup(self._reap, proc)
        out, errors = proc.communicate(timeout=timeout)
        print(f'\n--- POLLED: nvme amzn stats --interval 1 '
              f'{" ".join(args)} ---\nSTDOUT:\n{out}\nSTDERR:\n{errors}'
              '\n-----------------------------------', file=sys.stderr)
        return proc.returncode, out, errors

    @staticmethod
    def _reap(proc):
        """Leave no polling run behind, whatever the test did."""
        if proc.poll() is None:
            proc.kill()
            proc.wait()
