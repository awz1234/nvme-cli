#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of nvme-cli.
"""Tests for "nvme amzn id-ctrl", hardware-free.

The command is the core id-ctrl with one addition: it prints the block
device name the instance sees ("/dev/xvda", "/dev/nvme1n1"), which an
Amazon drive puts at the start of the vendor specific area of Identify
Controller. The field is space padded and need not be terminated, so what
is checked here is that the name comes out of it whole, that the rest of
the identify data is still reported as core reports it, and that the
padding never lands in the output.

Usage: python3 amzn_id_ctrl_mock_test.py <nvme-binary> <mock-lib>
"""
from tests.cli.nvme_mock import main
from amzn_mock_test import BDEV_SIZE, AmznMockTestBase

_COMMAND = 'id-ctrl'

# How the normal output labels the field.
_BDEV_PREFIX = 'bdev'


class TestAmznIdCtrl(AmznMockTestBase):
    """nvme amzn id-ctrl against a mocked Amazon drive."""

    def bdev_line(self, device=None):
        """The value on the bdev line of the normal output."""
        result = self.run_amzn_ok(_COMMAND, device=device)
        lines = [line for line in result.stdout.splitlines()
                 if line.startswith(_BDEV_PREFIX)]
        self.assertEqual(len(lines), 1,
                         f'expected one bdev line, got: {result.stdout!r}')
        key, _, value = lines[0].partition(':')
        self.assertEqual(key.strip(), _BDEV_PREFIX)
        return value.strip()

    # -- the block device name ---------------------------------------- #

    def test_bdev_is_read_from_the_vendor_area(self):
        """The name the drive reports is the name printed."""
        for bdev in ('/dev/xvda', '/dev/nvme1n1', '/dev/sdb'):
            with self.subTest(bdev=bdev):
                self.set_drive(bdev=bdev)

                self.assertEqual(self.bdev_line(), bdev)

    def test_json_reports_the_same_name(self):
        """The JSON output carries the name the normal output prints."""
        self.set_drive(bdev='/dev/nvme2n1')

        self.assertEqual(self.amzn_json(_COMMAND)['bdev'], '/dev/nvme2n1')

    def test_padding_is_not_part_of_the_name(self):
        """The field is space padded, and the padding is not printed."""
        self.set_drive(bdev='/dev/xvdf')
        result = self.run_amzn_ok(_COMMAND)

        self.assertIn('bdev', result.stdout)
        self.assertNotIn('/dev/xvdf ', result.stdout)
        self.assertEqual(self.amzn_json(_COMMAND)['bdev'], '/dev/xvdf')

    def test_long_name_is_not_run_past_the_field(self):
        """A name which fills the field has no padding to stop at. The
        plugin stops one byte short of the 32 the field holds, so the last
        character of a full-width name does not come out."""
        name = '/dev/disk/by-id/' + 'n' * (BDEV_SIZE - 16)
        self.assertEqual(len(name), BDEV_SIZE)
        self.set_drive(bdev=name)

        printed = self.bdev_line()
        self.assertEqual(printed, name[:BDEV_SIZE - 1])
        self.assertEqual(self.amzn_json(_COMMAND)['bdev'], printed)

    def test_empty_field_prints_no_name(self):
        """A drive which reports nothing leaves the name blank rather than
        printing the padding or reading past the field."""
        self.set_drive(bdev='')

        self.assertEqual(self.bdev_line(), '')
        self.assertEqual(self.amzn_json(_COMMAND)['bdev'].strip(), '')

    # -- still an id-ctrl --------------------------------------------- #

    def test_identify_data_is_reported_too(self):
        """The command reports the identify data, not only the name."""
        self.set_drive(bdev='/dev/xvda', vid=0x1d0f, sn='MOCKAMZN0001',
                       cntlid=0x21)
        data = self.amzn_json(_COMMAND)

        self.assertEqual(int(str(data['vid']), 0), 0x1d0f)
        self.assertEqual(int(str(data['cntlid']), 0), 0x21)
        self.assertEqual(data['sn'].strip(), 'MOCKAMZN0001')

    def test_namespace_path_reports_the_same_controller(self):
        """Either path identifies the same controller."""
        self.set_drive(bdev='/dev/nvme0n1', sn='MOCKAMZN0002')

        self.assertEqual(self.bdev_line(device=self.ctrl),
                         self.bdev_line(device=self.ns))

    def test_identify_is_sent_once(self):
        """The command needs one Identify Controller, not several."""
        self.run_amzn_ok(_COMMAND)

        self.assertEqual(len(self.server.identifies(cns=1)), 1)

    # -- failures ----------------------------------------------------- #

    def test_bad_device_returns_error(self):
        result = self.run_amzn(_COMMAND, device='/dev/nonexistent-nvme')

        self.assertNotEqual(result.returncode, 0)

    def test_invalid_output_format_returns_error(self):
        result = self.run_amzn(_COMMAND, '-o', 'notaformat')

        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    main()
