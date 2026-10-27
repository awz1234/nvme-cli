# SPDX-License-Identifier: GPL-2.0-or-later
#
# Hardware-free amzn plugin tests.

# Both suites check the JSON output as well, which needs json-c.
if have_mock_nvme and json_c_dep.found()
    amzn_mock_tests = [
        'amzn_id_ctrl_mock_test.py',
        'amzn_stats_mock_test.py',
    ]

    foreach t : amzn_mock_tests
        name = t.split('.')[0].replace('amzn_', '')
        name = name.replace('_mock_test', '').replace('_', '-')
        test('amzn - ' + name,
             python3,
             args: [files(t), nvme_exe, mock_nvme_lib],
             env: cli_test_env,
             depends: [nvme_exe, mock_nvme_lib],
             timeout: 120,
             suite: 'mock')
    endforeach
endif
