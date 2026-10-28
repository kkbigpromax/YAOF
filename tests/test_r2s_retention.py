import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("retention", Path(__file__).parents[1] / "SCRIPTS/11_retain_r2s_releases.py")
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)


def release(days, release_id=1):
    assets = []
    for day in days:
        for fs in ("ext4", "sfs"):
            assets.append({"id": len(assets) + 100 * release_id,
                           "name": f"R2S-GC404-2026-10-{day:02d}-25.12.6-{fs}.zip",
                           "created_at": f"2026-10-{day:02d}T12:00:00Z",
                           "state": "uploaded", "size": 20_000_000})
    return {"id": release_id, "assets": assets}


class RetentionTests(unittest.TestCase):
    def test_same_release_keeps_two_complete_versions(self):
        result = retention.plan([release([19, 20, 23, 27])])
        self.assertEqual(len(result["delete"]), 4)
        self.assertEqual({a["name"].split("-")[4] for a in result["keep"]}, {"23", "27"})

    def test_partial_or_failed_upload_does_not_evict_previous(self):
        data = release([20, 23, 27]);data["assets"].pop()
        result = retention.plan([data])
        self.assertFalse(result["delete"])
        data = release([20, 23, 27]);data["assets"][-1]["state"] = "new"
        self.assertFalse(retention.plan([data])["delete"])

    def test_no_deletion_with_fewer_than_three_versions(self):
        for days in ([], [20], [20, 23]):
            self.assertFalse(retention.plan([release(days)])["delete"])

    def test_other_devices_and_unrecognized_files_are_untouched(self):
        data = release([19, 20, 23]);extras = copy.deepcopy(data["assets"])
        for a in extras:a["name"] = a["name"].replace("R2S-", "R4S-")
        data["assets"] += extras
        result = retention.plan([data])
        self.assertEqual(len(result["delete"]), 2)
        self.assertTrue(all(a["name"].startswith("R2S-") for a in result["delete"]))

    def test_across_releases_and_drafts(self):
        a = release([19]);b = release([20, 23], 2);draft = release([27], 3);draft["draft"] = True
        result = retention.plan([a, b, draft])
        self.assertEqual({x["id"] for x in result["delete"]}, {100, 101})


if __name__ == "__main__":
    unittest.main()
