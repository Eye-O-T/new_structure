import unittest

from server.password_policy import validate_admin_password


class AdminPasswordPolicyTests(unittest.TestCase):
    def test_four_through_twelve_characters_are_valid(self):
        for password in ("1234", "123456789012"):
            validate_admin_password(password)

    def test_short_and_long_passwords_are_rejected(self):
        for password in ("123", "1234567890123"):
            with self.assertRaisesRegex(ValueError, "4 to 12"):
                validate_admin_password(password)


if __name__ == "__main__":
    unittest.main()
