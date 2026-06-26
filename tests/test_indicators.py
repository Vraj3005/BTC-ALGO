import unittest

import pandas as pd

from indicators import supertrend


class SupertrendTests(unittest.TestCase):
    def test_supertrend_runs_without_read_only_assignment_error(self):
        df = pd.DataFrame(
            {
                "High": [10, 11, 12, 13],
                "Low": [8, 9, 10, 11],
                "Close": [9, 10, 11, 12],
            }
        )

        line, direction = supertrend(df, period=2, multiplier=1)

        self.assertEqual(len(line), len(df))
        self.assertEqual(len(direction), len(df))
        self.assertTrue(line.iloc[0] == 0 or pd.notna(line.iloc[0]))


if __name__ == "__main__":
    unittest.main()
