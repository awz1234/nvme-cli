#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of nvme-cli.
"""Tests for "nvme amzn stats", hardware-free.

The command reads the Amazon statistics log page (0xD0) and prints the
counters, the read and write latency histograms, and with --details the
histogram of each IO size range. What it reads and prints depends on the
drive: instance storage returns the per-IO-size histograms as well, and
only an EBS volume reports the volume performance counters.

Every value checked here comes from the log page the mock returns, and no
two fields share a value, so a wrong offset or a swapped read and write
shows up. The counts in the log are the device's, so the tests also cover
what the plugin does with counts larger than its buffers.

Usage: python3 amzn_stats_mock_test.py <nvme-binary> <mock-lib>
"""
import json
import re

from tests.cli.nvme_mock import (CNS_CTRL, NVME_IOCTL_ID, NSID_ALL,
                                 SC_INVALID_FIELD, SC_INVALID_LOG_PAGE,
                                 Reply, main)
from amzn_mock_test import (BASE_SIZE, DEFAULT_BINS, DETAIL_BINS,
                            EBS_MODEL, LID_STATS, LOCAL_STORAGE_MAGIC,
                            LOCAL_STORAGE_MODEL, NUM_HISTOGRAM, PAGE_SIZE,
                            AmznMockTestBase, FailAfter, detail_counts,
                            pack_id_ctrl, pack_stats_log)

_COMMAND = 'stats'

# Counters of a drive which has done some work: every one different.
_COUNTERS = {
    'total_read_ops': 1111,
    'total_write_ops': 2222,
    'total_read_bytes': 3333333,
    'total_write_bytes': 4444444,
    'total_read_time': 555555,
    'total_write_time': 666666,
    'ebs_volume_performance_exceeded_iops': 77,
    'ebs_volume_performance_exceeded_tp': 88,
    'ec2_instance_performance_exceeded_iops': 99,
    'ec2_instance_performance_exceeded_tp': 111,
    'volume_queue_length': 12,
}

# How the normal output names the counters, in the order it prints them.
_READ_WRITE_SECTIONS = (
    ('Total Ops', 'total_read_ops', 'total_write_ops'),
    ('Total Bytes', 'total_read_bytes', 'total_write_bytes'),
    ('Total Time (us)', 'total_read_time', 'total_write_time'),
)

_EBS_EXCEEDED = 'EBS Volume Performance Exceeded (us)'
_INSTANCE_EBS = 'EC2 Instance EBS Performance Exceeded (us)'
_INSTANCE_LOCAL = 'EC2 Instance Local Storage Performance Exceeded (us)'
_QUEUE_LENGTH = 'Queue Length (point in time)'

_READ_HISTOGRAM = 'Read IO Latency Histogram'
_WRITE_HISTOGRAM = 'Write IO Latency Histogram'

_NOT_EC2 = 'Not an EC2 device'

# The first line of a report, which each poll of --interval starts with.
_REPORT_START = 'Total Ops:'

# "[lower    - upper   ] => count" of a histogram row.
_BIN_RE = re.compile(r'^\[(\d+)\s*-\s*(\d+)\s*\] => (\d+)\s*$')
# "(io_size_low: 4K -> io_size_high: 8K]" of a --details section.
_IO_SIZE_RE = re.compile(r'io_size_low: (\S+) -> io_size_high: ([^\]]+)\]')


def section_values(text, title):
    """The "  Read: 1" / "  Write: 2" pair under a section title."""
    lines = text.splitlines()
    start = lines.index(f'{title}:')
    values = {}
    for line in lines[start + 1:]:
        if not line.startswith('  '):
            break
        key, _, value = line.partition(':')
        values[key.strip()] = value.strip()
    return values


def later_report(text, number=2):
    """The output from the start of the @number-th report on. The reports
    of a polling run follow each other on stdout."""
    position = -1
    for _ in range(number):
        position = text.index(_REPORT_START, position + 1)
    return text[position:]


def json_documents(text):
    """The JSON objects of an output which holds several, one per poll.
    They are printed one after the other, each starting in column 0."""
    documents = []
    current = []
    for line in text.splitlines():
        if line.startswith('{'):
            current = [line]
        elif current:
            current.append(line)
            if line.startswith('}'):
                documents.append(json.loads('\n'.join(current)))
                current = []
    return documents


def detail_sections(text):
    """The --details output: [(low, high, read rows, write rows)] of each
    IO size range. The per-range histograms carry the same two headings as
    the histograms of the whole drive, which are printed above the first
    range, so the ranges delimit them."""
    lines = text.splitlines()
    sections = []
    index = 0
    while index < len(lines):
        match = _IO_SIZE_RE.search(lines[index])
        index += 1
        if not match:
            continue
        rows = {_READ_HISTOGRAM: [], _WRITE_HISTOGRAM: []}
        which = None
        while index < len(lines) and not _IO_SIZE_RE.search(lines[index]):
            if lines[index] in rows:
                which = lines[index]
            else:
                row = _BIN_RE.match(lines[index])
                if row and which:
                    rows[which].append(tuple(int(group)
                                             for group in row.groups()))
            index += 1
        sections.append((*match.groups(), rows[_READ_HISTOGRAM],
                         rows[_WRITE_HISTOGRAM]))
    return sections


def histogram_rows(text, title):
    """The (lower, upper, count) rows of a named histogram."""
    lines = text.splitlines()
    rows = []
    for index, line in enumerate(lines):
        if line != title:
            continue
        for row in lines[index + 1:]:
            match = _BIN_RE.match(row)
            if match:
                rows.append(tuple(int(group) for group in match.groups()))
            elif rows:
                break
        break
    return rows


class TestAmznStats(AmznMockTestBase):
    """nvme amzn stats against a mocked Amazon drive."""

    # -- the counters ------------------------------------------------- #

    def test_counters_are_read_from_the_log(self):
        """Each counter is printed with the value the log holds."""
        self.set_stats(**_COUNTERS)
        result = self.run_amzn_ok(_COMMAND)

        for title, read, write in _READ_WRITE_SECTIONS:
            with self.subTest(section=title):
                values = section_values(result.stdout, title)
                self.assertEqual(values['Read'], str(_COUNTERS[read]))
                self.assertEqual(values['Write'], str(_COUNTERS[write]))

    def test_json_reports_the_counters(self):
        """The JSON output carries the same counters under their own
        names."""
        self.set_stats(**_COUNTERS)
        data = self.amzn_json(_COMMAND)

        for name, value in _COUNTERS.items():
            with self.subTest(field=name):
                self.assertEqual(int(str(data[name]), 0), value)

    def test_queue_length_is_reported(self):
        """The queue length is a point-in-time value of its own."""
        self.set_stats(volume_queue_length=42)
        result = self.run_amzn_ok(_COMMAND)

        self.assertIn(f'{_QUEUE_LENGTH}: 42', result.stdout)

    def test_large_counters_keep_their_value(self):
        """A 64 bit counter is not truncated on the way out."""
        big = (1 << 64) - 1
        self.set_stats(total_read_ops=big, total_write_bytes=big - 1)

        data = self.amzn_json(_COMMAND)
        self.assertEqual(int(str(data['total_read_ops']), 0), big)
        self.assertEqual(int(str(data['total_write_bytes']), 0), big - 1)

    # -- EBS volume and instance storage ------------------------------ #

    def test_ebs_volume_reports_the_volume_counters(self):
        """An EBS volume has volume performance counters, and its own
        heading for the instance ones."""
        self.set_drive(model=EBS_MODEL)
        self.set_stats(**_COUNTERS)
        result = self.run_amzn_ok(_COMMAND)

        self.assertIn(_EBS_EXCEEDED, result.stdout)
        self.assertIn(_INSTANCE_EBS, result.stdout)
        self.assertNotIn(_INSTANCE_LOCAL, result.stdout)
        values = section_values(result.stdout, _EBS_EXCEEDED)
        self.assertEqual(
            values['IOPS'],
            str(_COUNTERS['ebs_volume_performance_exceeded_iops']))
        self.assertEqual(
            values['Throughput'],
            str(_COUNTERS['ebs_volume_performance_exceeded_tp']))

    def test_instance_storage_reports_its_own_heading(self):
        """Instance storage has no volume behind it: the volume counters
        are left out and the instance heading names local storage."""
        self.set_drive(model=LOCAL_STORAGE_MODEL)
        self.set_stats(magic=LOCAL_STORAGE_MAGIC, **_COUNTERS)
        result = self.run_amzn_ok(_COMMAND)

        self.assertNotIn(_EBS_EXCEEDED, result.stdout)
        self.assertNotIn(_INSTANCE_EBS, result.stdout)
        values = section_values(result.stdout, _INSTANCE_LOCAL)
        self.assertEqual(
            values['IOPS'],
            str(_COUNTERS['ec2_instance_performance_exceeded_iops']))

    def log_bytes_read(self):
        """How much of the log page the run asked for. A log is read in
        chunks of NVME_LOG_PAGE_PDU_SIZE, so a larger one takes several
        commands."""
        return sum(read.data_len for read in self.stats_log_reads())

    def test_ebs_volume_reads_only_the_base_of_the_log(self):
        """An EBS volume has no per-IO-size histograms, so the command
        asks for the first half of the page only."""
        self.set_drive(model=EBS_MODEL)
        self.run_amzn_ok(_COMMAND)

        self.assertEqual(self.log_bytes_read(), BASE_SIZE)

    def test_instance_storage_reads_the_whole_log(self):
        """Instance storage returns the per-IO-size histograms too."""
        self.set_drive(model=LOCAL_STORAGE_MODEL)
        self.set_stats(magic=LOCAL_STORAGE_MAGIC)
        self.run_amzn_ok(_COMMAND)

        self.assertEqual(self.log_bytes_read(), PAGE_SIZE)

    def test_instance_storage_uses_the_namespace_of_the_device(self):
        """The log is read for the namespace the device names."""
        self.set_drive(model=LOCAL_STORAGE_MODEL)
        self.set_stats(magic=LOCAL_STORAGE_MAGIC)
        self.server.ioctls[NVME_IOCTL_ID] = Reply(status=3)
        self.run_amzn_ok(_COMMAND, device=self.ns)

        self.assertEqual(self.stats_log_reads()[0].nsid, 3)

    def test_instance_storage_falls_back_to_all_namespaces(self):
        """Asked on a controller, which has no namespace id, the command
        reads the log for every namespace."""
        self.set_drive(model=LOCAL_STORAGE_MODEL)
        self.set_stats(magic=LOCAL_STORAGE_MAGIC)
        self.run_amzn_ok(_COMMAND, device=self.ctrl)

        self.assertEqual(self.stats_log_reads()[0].nsid, NSID_ALL)

    # -- the latency histograms --------------------------------------- #

    def test_histograms_are_read_from_the_log(self):
        """Both histograms are printed with the bins of the log, and the
        read one is not the write one."""
        write_bins = tuple((lower, upper, count * 2)
                           for lower, upper, count in DEFAULT_BINS)
        self.set_stats(read_bins=DEFAULT_BINS, write_bins=write_bins)
        result = self.run_amzn_ok(_COMMAND)

        self.assertEqual(histogram_rows(result.stdout, _READ_HISTOGRAM),
                         list(DEFAULT_BINS))
        self.assertEqual(histogram_rows(result.stdout, _WRITE_HISTOGRAM),
                         list(write_bins))

    def test_json_reports_the_histograms(self):
        """The JSON output carries each histogram with its bins."""
        self.set_stats(read_bins=DEFAULT_BINS)
        data = self.amzn_json(_COMMAND)

        histogram = data['read_io_latency_histogram']
        self.assertEqual(int(str(histogram['num_bins']), 0),
                         len(DEFAULT_BINS))
        bins = [(int(str(b['lower']), 0), int(str(b['upper']), 0),
                 int(str(b['count']), 0)) for b in histogram['bins']]
        self.assertEqual(bins, list(DEFAULT_BINS))

    def test_bin_count_beyond_the_buffer_is_capped(self):
        """The count comes from the device: a count past the bins the log
        can hold must not read past them."""
        self.set_stats(read_bins=DEFAULT_BINS, num_read_bins=1000)
        result = self.run_amzn_ok(_COMMAND)

        rows = histogram_rows(result.stdout, _READ_HISTOGRAM)
        self.assertEqual(len(rows), DETAIL_BINS)
        self.assertEqual(rows[:len(DEFAULT_BINS)], list(DEFAULT_BINS))

    def test_write_bin_count_beyond_the_buffer_is_capped(self):
        """Each histogram carries its own count, and both are capped."""
        self.set_stats(write_bins=DEFAULT_BINS, num_write_bins=1000)
        result = self.run_amzn_ok(_COMMAND)

        self.assertEqual(
            len(histogram_rows(result.stdout, _WRITE_HISTOGRAM)),
            DETAIL_BINS)
        self.assertEqual(
            len(histogram_rows(result.stdout, _READ_HISTOGRAM)),
            len(DEFAULT_BINS))

    def test_empty_histogram_prints_no_bins(self):
        """A drive which reports no bins gets no rows, and no error."""
        self.set_stats(read_bins=(), write_bins=())
        result = self.run_amzn_ok(_COMMAND)

        self.assertEqual(histogram_rows(result.stdout, _READ_HISTOGRAM), [])
        self.assertIn(_READ_HISTOGRAM, result.stdout)

    # -- the per-IO-size histograms (--details) ----------------------- #

    def detail_log(self, **kwargs):
        """Instance storage with per-IO-size histograms: histogram N has
        counts starting at N * 100, so each one is distinguishable.

        A per-IO-size histogram has no bounds of its own: the plugin takes
        them, and how many bins to report, from the base write histogram.
        So only its first len(write_bins) counts are ever reported, which
        is what @detail_bins holds."""
        detail = {index: (detail_counts(first=100 * index + 1),
                          detail_counts(first=100 * index + 51))
                  for index in range(NUM_HISTOGRAM)}
        self.set_drive(model=LOCAL_STORAGE_MODEL)
        self.set_stats(magic=LOCAL_STORAGE_MAGIC, detail=detail, **kwargs)
        self.detail_bins = len(kwargs.get('write_bins', DEFAULT_BINS))
        return detail

    def test_details_follow_the_base_write_histogram(self):
        """The bounds of a per-IO-size histogram are those of the base
        write histogram, and it reports as many bins as that one."""
        bins = ((0, 10, 1), (10, 20, 2))
        self.detail_log(write_bins=bins, io_sizes=(4096,), num_of_hists=1)
        result = self.run_amzn_ok(_COMMAND, '-d')

        sections = detail_sections(result.stdout)
        self.assertEqual(len(sections), 1)
        _, _, read_rows, write_rows = sections[0]
        bounds = [(lower, upper) for lower, upper, _ in bins]
        self.assertEqual([(row[0], row[1]) for row in read_rows], bounds)
        self.assertEqual([(row[0], row[1]) for row in write_rows], bounds)

    def test_details_are_printed_only_when_asked(self):
        """The per-IO-size histograms are behind --details."""
        self.detail_log()

        without = self.run_amzn_ok(_COMMAND)
        self.assertNotIn('IO Size Range', without.stdout)

        with_details = self.run_amzn_ok(_COMMAND, '--details')
        self.assertIn('IO Size Range', with_details.stdout)

    def test_details_report_each_io_size_range(self):
        """Each histogram covers the range between two reported IO sizes.
        A size is printed in the largest unit it is a whole number of, and
        in bytes when it is neither a whole number of KiB nor of MiB;
        there is no upper bound on the last range."""
        self.detail_log(io_sizes=(1000, 4096, 1048576, 0xffffffff),
                        num_of_hists=4)
        result = self.run_amzn_ok(_COMMAND, '-d')

        ranges = [(low, high) for low, high, _, _
                  in detail_sections(result.stdout)]
        self.assertEqual(ranges, [('0', '1000'), ('1000', '4K'),
                                  ('4K', '1M'), ('1M', 'max')])

    def test_details_report_the_counts_of_each_histogram(self):
        """The counts of a range are its own, read and write apart."""
        detail = self.detail_log(io_sizes=(4096, 8192), num_of_hists=2)
        result = self.run_amzn_ok(_COMMAND, '-d')

        sections = detail_sections(result.stdout)
        self.assertEqual(len(sections), 2)
        for index, (_, _, read_rows, write_rows) in enumerate(sections):
            with self.subTest(histogram=index):
                reads, writes = detail[index]
                self.assertEqual([row[2] for row in read_rows],
                                 reads[:self.detail_bins])
                self.assertEqual([row[2] for row in write_rows],
                                 writes[:self.detail_bins])

    def test_json_reports_the_details(self):
        """The JSON output nests the per-IO-size histograms under their
        own member, read and write apart."""
        detail = self.detail_log(io_sizes=(4096, 8192), num_of_hists=2)
        data = self.amzn_json(_COMMAND, '-d')

        histograms = data['latency_histograms']
        self.assertEqual(int(str(histograms['num_of_hists']), 0), 2)
        first = histograms['io_histograms'][0]
        self.assertEqual(first['io_size_low'], '0')
        self.assertEqual(first['io_size_high'], '4K')
        for name, counts in (('read_io_latency_histogram', detail[0][0]),
                             ('write_io_latency_histogram', detail[0][1])):
            with self.subTest(histogram=name):
                reported = [int(str(b['count']), 0)
                            for b in first[name]['bins']]
                self.assertEqual(reported, counts[:self.detail_bins])

    def test_details_are_left_out_of_an_older_log(self):
        """A log page which predates the per-IO-size histograms has none,
        whether or not they are asked for."""
        self.detail_log(version=0)
        result = self.run_amzn_ok(_COMMAND, '--details')

        self.assertNotIn('IO Size Range', result.stdout)

    def test_histogram_count_beyond_the_buffer_is_capped(self):
        """The number of histograms comes from the device too."""
        self.detail_log(num_of_hists=1000)
        result = self.run_amzn_ok(_COMMAND, '-d')

        self.assertEqual(len(detail_sections(result.stdout)),
                         NUM_HISTOGRAM)

    # -- polling (--interval) ----------------------------------------- #

    def polling_pages(self, first, second, **kwargs):
        """Two polls of a drive which did some work in between."""
        return self.set_stats_sequence([
            pack_stats_log(total_read_ops=first, total_write_ops=first * 2,
                           total_read_bytes=first * 512,
                           volume_queue_length=first, **kwargs),
            pack_stats_log(total_read_ops=second, total_write_ops=second * 2,
                           total_read_bytes=second * 512,
                           volume_queue_length=second, **kwargs),
        ])

    def test_polling_reports_the_first_poll_in_full(self):
        """The first report is the state of the drive, not a difference."""
        self.polling_pages(100, 175)
        text, _ = self.poll_stats()

        first = section_values(text, 'Total Ops')
        self.assertEqual(first['Read'], '100')
        self.assertEqual(first['Write'], '200')

    def test_polling_reports_what_changed_since_the_previous_poll(self):
        """Every later report holds the difference, counter by counter."""
        self.polling_pages(100, 175)
        text, _ = self.poll_stats()

        second = later_report(text)
        ops = section_values(second, 'Total Ops')
        self.assertEqual(ops['Read'], '75')
        self.assertEqual(ops['Write'], '150')
        self.assertEqual(section_values(second, 'Total Bytes')['Read'],
                         str(75 * 512))

    def test_polling_keeps_the_queue_length_as_it_is(self):
        """The queue length is a point-in-time value: the report holds the
        value of the poll, not the difference."""
        self.polling_pages(100, 175)
        text, _ = self.poll_stats()

        lengths = re.findall(rf'{re.escape(_QUEUE_LENGTH)}: (\d+)', text)
        self.assertEqual(lengths[:2], ['100', '175'])

    def test_polling_diffs_the_histograms(self):
        """A histogram keeps its bounds and reports the count of the
        interval."""
        bins = ((0, 64, 10), (64, 128, 20))
        later = ((0, 64, 13), (64, 128, 45))
        self.set_stats_sequence([
            pack_stats_log(read_bins=bins, write_bins=bins),
            pack_stats_log(read_bins=later, write_bins=later),
        ])
        text, _ = self.poll_stats()

        rows = histogram_rows(later_report(text), _READ_HISTOGRAM)
        self.assertEqual(rows, [(0, 64, 3), (64, 128, 25)])

    def test_polling_diffs_the_details_of_instance_storage(self):
        """The per-IO-size histograms are diffed as well."""
        before = {0: (detail_counts(first=1), detail_counts(first=101))}
        after = {0: (detail_counts(first=11), detail_counts(first=131))}
        self.set_drive(model=LOCAL_STORAGE_MODEL)
        self.set_stats_sequence([
            pack_stats_log(magic=LOCAL_STORAGE_MAGIC, detail=before,
                           io_sizes=(4096,), num_of_hists=1),
            pack_stats_log(magic=LOCAL_STORAGE_MAGIC, detail=after,
                           io_sizes=(4096,), num_of_hists=1),
        ])
        text, _ = self.poll_stats('--details')

        sections = detail_sections(later_report(text))
        self.assertEqual(len(sections), 1)
        _, _, read_rows, write_rows = sections[0]
        # Every bin grew by the difference between the two pages, and the
        # range reports as many bins as the base write histogram.
        self.assertEqual(len(read_rows), len(DEFAULT_BINS))
        self.assertEqual([row[2] for row in read_rows],
                         [10] * len(DEFAULT_BINS))
        self.assertEqual([row[2] for row in write_rows],
                         [30] * len(DEFAULT_BINS))

    def test_polling_announces_itself_and_stops_on_interrupt(self):
        """The command says it is polling, and Ctrl+C ends it cleanly."""
        self.polling_pages(100, 175)
        text, _ = self.poll_stats()

        self.assertIn('Polling NVMe stats every 1 sec(s)', text)
        self.assertGreaterEqual(len(self.stats_log_reads()), 2)

    def test_polling_reports_json(self):
        """Each poll is a JSON document of its own, the first the state of
        the drive and the next the difference."""
        self.polling_pages(100, 175)
        text, _ = self.poll_stats('-o', 'json')

        documents = json_documents(text)
        self.assertGreaterEqual(len(documents), 2)
        self.assertEqual(int(str(documents[0]['total_read_ops']), 0), 100)
        self.assertEqual(int(str(documents[1]['total_read_ops']), 0), 75)

    # -- failures ----------------------------------------------------- #

    def test_other_vendors_drive_is_refused(self):
        """A log page without an Amazon magic is not this drive's."""
        self.set_stats(magic=0xdeadbeef)
        result = self.run_amzn(_COMMAND)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(_NOT_EC2, result.stderr)

    def test_log_page_failure_is_reported(self):
        """A drive which does not have the log page fails with a
        message."""
        self.server.logs[LID_STATS] = SC_INVALID_LOG_PAGE
        result = self.run_amzn(_COMMAND)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Failed to get log page', result.stderr)

    def test_polling_log_page_failure_ends_the_run(self):
        """A drive which stops answering ends the polling instead of
        looping on the error."""
        self.set_stats_sequence([pack_stats_log(total_read_ops=100),
                                 SC_INVALID_LOG_PAGE])
        returncode, out, errors = self.poll_stats_until_exit()

        self.assertNotEqual(returncode, 0)
        self.assertIn('get log page failed', errors)
        # The report of the first poll was printed before the failure.
        self.assertIn(_REPORT_START, out)

    def test_instance_storage_second_identify_failure_is_reported(self):
        """Asked on a controller, instance storage identifies it a second
        time to settle the namespace id. A drive which stops answering
        ends the command rather than reading the log anyway."""
        self.server.identify[CNS_CTRL] = FailAfter(
            pack_id_ctrl(model=LOCAL_STORAGE_MODEL), SC_INVALID_FIELD)
        result = self.run_amzn(_COMMAND, device=self.ctrl)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.stats_log_reads(), [])

    def test_identify_failure_is_reported(self):
        """The model name decides what to read, so the command needs the
        identify data first."""
        self.server.identify[1] = SC_INVALID_LOG_PAGE
        result = self.run_amzn(_COMMAND)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Failed to get identify controller', result.stderr)

    def test_binary_output_format_is_refused(self):
        """The command prints text or JSON, so binary is refused rather
        than quietly printing text."""
        result = self.run_amzn(_COMMAND, '-o', 'binary')

        self.assertNotEqual(result.returncode, 0)

    def test_invalid_output_format_returns_error(self):
        result = self.run_amzn(_COMMAND, '-o', 'notaformat')

        self.assertNotEqual(result.returncode, 0)

    def test_bad_device_returns_error(self):
        result = self.run_amzn(_COMMAND, device='/dev/nonexistent-nvme')

        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    main()
