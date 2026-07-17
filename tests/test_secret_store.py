import json
import tempfile
import unittest
from pathlib import Path

from src.secret_store import EncryptedSecretStore, SecretStoreError


RUNTIME_TEST_DIR = Path(__file__).resolve().parents[1] / ".runtime" / "tests"


class EncryptedSecretStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME_TEST_DIR.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=RUNTIME_TEST_DIR)
        self.path = Path(self.temporary.name) / "secrets.enc"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_trip_is_encrypted_and_permission_restricted(self) -> None:
        store = EncryptedSecretStore(self.path, token="a" * 64)
        store.set_many({"service": {"api_key": "secret-value"}})
        self.assertNotIn(b"secret-value", self.path.read_bytes())
        self.assertEqual(
            store.get_many(["service"])["service"]["api_key"],
            "secret-value",
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_wrong_key_fails_closed(self) -> None:
        EncryptedSecretStore(self.path, token="a" * 64).set_many({"key": "value"})
        with self.assertRaises(SecretStoreError):
            EncryptedSecretStore(self.path, token="b" * 64).get_many(["key"])

    def test_empty_store_needs_no_key(self) -> None:
        EncryptedSecretStore(self.path, token="").set_many({"missing": None})
        self.assertFalse(self.path.exists())

    def test_parent_symlink_and_hard_linked_store_are_rejected(self) -> None:
        outside_dir = Path(self.temporary.name) / "outside"
        outside_dir.mkdir()
        linked_parent = Path(self.temporary.name) / "linked"
        linked_parent.symlink_to(outside_dir, target_is_directory=True)
        linked_store = EncryptedSecretStore(
            linked_parent / "secrets.enc", token="a" * 64
        )
        with self.assertRaises(SecretStoreError):
            linked_store.set_many({"key": "value"})
        self.assertEqual(list(outside_dir.iterdir()), [])

        original_store = EncryptedSecretStore(self.path, token="a" * 64)
        original_store.set_many({"key": "value"})
        outside_copy = Path(self.temporary.name) / "outside-copy.enc"
        outside_copy.hardlink_to(self.path)
        with self.assertRaises(SecretStoreError):
            original_store.get_many(["key"])


if __name__ == "__main__":
    unittest.main()
