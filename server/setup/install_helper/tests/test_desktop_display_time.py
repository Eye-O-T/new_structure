import unittest

from server.desktop.admin.display_time import format_kst


class DesktopDisplayTimeTests(unittest.TestCase):
    def test_utc_timestamp_is_converted_to_kst(self):
        self.assertEqual(
            format_kst("2026-11-04T05:14:54.226Z"),
            "2026-11-04 14:14:54 KST",
        )

    def test_conversion_rolls_over_to_the_next_day(self):
        self.assertEqual(
            format_kst("2026-11-04T18:30:00Z"),
            "2026-11-05 03:30:00 KST",
        )

    def test_equivalent_offsets_produce_the_same_local_time(self):
        expected = "2026-11-04 14:14:54 KST"
        self.assertEqual(format_kst("2026-11-04T05:14:54+00:00"), expected)
        self.assertEqual(format_kst("2026-11-04T14:14:54+09:00"), expected)

    def test_missing_invalid_and_naive_times_are_explicit(self):
        self.assertEqual(format_kst(None), "시각 확인 불가")
        self.assertEqual(format_kst("invalid"), "시각 확인 불가")
        self.assertEqual(format_kst("2026-11-04T05:14:54"), "시각 확인 불가")
        self.assertEqual(format_kst("-"), "-")


if __name__ == "__main__":
    unittest.main()
