"""The sidecar schema is the contract between three writers that never run
together: the uploader, the processor and the write API. A change that breaks
it shows up as silently missing metadata, not an error."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for _package in ("media_processor", "api"):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "lambdas", _package))

from models import ArchiveEntry, Manifest, MediaRecord  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from schemas import CategoryRequest, KeysRequest, RenameRequest  # noqa: E402


class Sidecar(unittest.TestCase):
    def test_uploader_writes_the_minimum_and_it_validates(self):
        record = MediaRecord(key="media/ab.jpg", name="IMG.jpg", source_path="Berlin/IMG.jpg",
                             category="Berlin", size=10)
        self.assertEqual(record.kind, "other")
        self.assertFalse(record.renderable)
        self.assertFalse(record.archived)

    def test_unknown_fields_from_an_older_sidecar_are_ignored(self):
        record = MediaRecord.model_validate(
            {"key": "k", "name": "n", "legacy_field": "whatever"})
        self.assertEqual(record.key, "k")

    def test_sort_key_prefers_capture_over_upload_time(self):
        both = MediaRecord(key="k", name="n", taken_at="2019", modified_at="2026")
        self.assertEqual(both.sort_key(), "2019")
        neither = MediaRecord(key="k", name="n")
        self.assertEqual(neither.sort_key(), "")

    def test_none_fields_are_dropped_on_serialisation(self):
        # Keeps sidecars small and avoids writing nulls the frontend must guard.
        body = MediaRecord(key="k", name="n").to_json().decode()
        self.assertNotIn("taken_at", body)
        self.assertIn('"key":"k"', body)

    def test_a_bad_kind_is_rejected(self):
        with self.assertRaises(ValidationError):
            MediaRecord(key="k", name="n", kind="spreadsheet")


class ManifestShape(unittest.TestCase):
    def test_empty_manifest_serialises(self):
        self.assertIn("generated_at", Manifest(generated_at="now").to_json().decode())

    def test_archived_entry_keeps_its_media_key(self):
        entry = ArchiveEntry(key="media/ab.mp4", name="a.mp4", storage_class="archived",
                             kind="video", playable=True)
        self.assertTrue(entry.key.startswith("media/"))
        self.assertTrue(entry.playable)


class Requests(unittest.TestCase):
    def test_empty_key_list_is_rejected(self):
        with self.assertRaises(ValidationError):
            KeysRequest(keys=[])

    def test_category_length_is_bounded(self):
        with self.assertRaises(ValidationError):
            CategoryRequest(keys=["media/a.jpg"], category="x" * 200)

    def test_blank_category_is_rejected(self):
        with self.assertRaises(ValidationError):
            CategoryRequest(keys=["media/a.jpg"], category="")

    def test_rename_accepts_the_wire_names(self):
        # "from" is a python keyword, so the field is aliased.
        request = RenameRequest.model_validate({"from": "Ofer Trip", "to": "China 2017"})
        self.assertEqual(request.source, "Ofer Trip")
        self.assertEqual(request.target, "China 2017")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RebuildContract(unittest.TestCase):
    """rebuild() returns a Manifest model, not a dict. Callers that subscript it
    fail only at runtime, which is how 'Manifest object is not subscriptable'
    reached a deployed function."""

    def test_manifest_is_attribute_accessed_not_subscripted(self):
        manifest = Manifest(generated_at="now", items=[MediaRecord(key="k", name="n")])
        self.assertEqual(len(manifest.items), 1)
        with self.assertRaises(TypeError):
            manifest["items"]


class Favourites(unittest.TestCase):
    """Favourites are per user, so they live in their own object and never
    enter the shared manifest — otherwise one person's hearts would show to
    everyone and the manifest would stop being identical for all viewers."""

    def test_each_user_gets_their_own_object(self):
        import mutations
        mine = mutations.prefs_key("a@example.com")
        theirs = mutations.prefs_key("b@example.com")
        self.assertNotEqual(mine, theirs)
        self.assertTrue(mine.startswith("prefs/"))

    def test_the_key_does_not_leak_the_email(self):
        import mutations
        self.assertNotIn("a@example.com", mutations.prefs_key("a@example.com"))

    def test_favourite_request_defaults_to_on(self):
        from schemas import FavouriteRequest
        self.assertTrue(FavouriteRequest(keys=["media/a.jpg"]).on)
        self.assertFalse(FavouriteRequest(keys=["media/a.jpg"], on=False).on)


class Dates(unittest.TestCase):
    def test_filename_dates_are_recovered(self):
        import filename_dates
        self.assertEqual(filename_dates.from_name("PXL_20220928_154721228.mp4"),
                         "2022-09-28T00:00:00Z")
        self.assertEqual(filename_dates.from_name("2017-03-25 20.37.10.jpg"),
                         "2017-03-25T00:00:00Z")

    def test_a_name_with_no_date_yields_nothing(self):
        import filename_dates
        self.assertIsNone(filename_dates.from_name("GOPR0137.mp4"))
        self.assertIsNone(filename_dates.from_name("YDXJ0998.MP4"))

    def test_implausible_dates_are_refused(self):
        # A camera without a clock writes 1970, which would bury the item.
        import filename_dates
        self.assertIsNone(filename_dates.from_name("1970-01-01 00.33.37.mp4"))

    def test_sort_key_prefers_source_mtime_over_upload_time(self):
        record = MediaRecord(key="k", name="n", source_mtime="2017", modified_at="2026")
        self.assertEqual(record.sort_key(), "2017")


class Hiding(unittest.TestCase):
    def test_hide_request_needs_a_category(self):
        from schemas import HideRequest
        with self.assertRaises(ValidationError):
            HideRequest(categories=[])
        self.assertTrue(HideRequest(categories=["Ofer Trip"]).on)


class EpochDates(unittest.TestCase):
    def test_a_1970_stamp_is_skipped_for_the_next_best(self):
        record = MediaRecord(key="k", name="n", taken_at="1970-01-01T00:33:37Z",
                             source_mtime="2017-12-17T00:00:00Z")
        self.assertTrue(record.sort_key().startswith("2017"))

    def test_all_implausible_falls_back_to_upload_time(self):
        record = MediaRecord(key="k", name="n", source_mtime="1970-01-01T00:00:00Z",
                             modified_at="2026-09-19T00:00:00Z")
        self.assertTrue(record.sort_key().startswith("2026"))
