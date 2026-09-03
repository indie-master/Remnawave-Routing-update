import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path

from app import (
    Config,
    build_settings_payload,
    current_settings_routing,
    decode_deeplink,
    should_update,
)


ALLOWED_HOSTS = frozenset({"cdn.jsdelivr.net"})


def make_deeplink(timestamp=100, name="RoscomVPN"):
    profile = {
        "Name": name,
        "LastUpdated": str(timestamp),
        "Geoipurl": "https://cdn.jsdelivr.net/gh/indie-master/happ-routing@tag/release/geoip.dat",
        "Geositeurl": "https://cdn.jsdelivr.net/gh/indie-master/happ-routing@tag/release/geosite.dat",
        "DirectSites": ["geosite:wechat"],
        "DirectIp": ["geoip:wechat"],
        "ProxySites": ["geosite:youtube"],
        "BlockSites": ["geosite:torrent"],
    }
    encoded = base64.b64encode(
        json.dumps(profile, separators=(",", ":")).encode()
    ).decode()
    return f"happ://routing/add/{encoded}", profile


def make_config(tmpdir, target="response-rule"):
    return Config(
        remna_base_url="http://remnawave:3000/api",
        remna_token="token",
        github_raw_url="https://raw.githubusercontent.com/example/profile",
        update_target=target,
        response_rule_name="Happ",
        check_interval=300,
        cron_schedule="",
        ssl_verify=True,
        dry_run=True,
        validate_geo_urls=True,
        allow_profile_rename=False,
        backup_dir=Path(tmpdir),
        allowed_geo_hosts=ALLOWED_HOSTS,
        request_timeout=30,
        squads=(),
    )


class DeeplinkTests(unittest.TestCase):
    def test_valid_deeplink(self):
        deeplink, expected = make_deeplink()
        self.assertEqual(decode_deeplink(deeplink, ALLOWED_HOSTS), expected)

    def test_disallowed_geodata_host(self):
        deeplink, _ = make_deeplink()
        with self.assertRaisesRegex(ValueError, "disallowed URL"):
            decode_deeplink(deeplink, frozenset({"example.com"}))

    def test_monotonic_update(self):
        old, _ = make_deeplink(100)
        new, profile = make_deeplink(101)
        self.assertTrue(
            should_update(old, new, profile, False, ALLOWED_HOSTS)[0]
        )
        stale, stale_profile = make_deeplink(99)
        self.assertFalse(
            should_update(old, stale, stale_profile, False, ALLOWED_HOSTS)[0]
        )

    def test_profile_rename_is_rejected(self):
        old, _ = make_deeplink(100, "RoscomVPN")
        new, profile = make_deeplink(101, "Other")
        with self.assertRaisesRegex(ValueError, "Refusing profile rename"):
            should_update(old, new, profile, False, ALLOWED_HOSTS)


class ResponseRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmpdir.name)
        self.old, _ = make_deeplink(100)
        self.new, _ = make_deeplink(101)
        self.settings = {
            "uuid": "19f856a3-df14-4cfa-b2e5-51061daa250d",
            "responseRules": {
                "version": "1",
                "rules": [
                    {
                        "name": "Happ",
                        "enabled": True,
                        "responseModifications": {
                            "headers": [
                                {"key": "X-Test", "value": "preserve"},
                                {"key": "Routing", "value": self.old},
                            ],
                            "applyHeadersToEnd": True,
                        },
                    },
                    {
                        "name": "Other",
                        "enabled": True,
                        "responseModifications": {
                            "headers": [
                                {"key": "X-Other", "value": "untouched"}
                            ]
                        },
                    },
                ],
            },
        }

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_only_named_rule_header_is_changed(self):
        original = copy.deepcopy(self.settings)
        payload = build_settings_payload(self.settings, self.config, self.new)
        self.assertEqual(set(payload), {"uuid", "responseRules"})
        self.assertEqual(current_settings_routing(self.settings, self.config), self.old)
        updated = {
            "uuid": self.settings["uuid"],
            "responseRules": payload["responseRules"],
        }
        self.assertEqual(current_settings_routing(updated, self.config), self.new)
        rules = payload["responseRules"]["rules"]
        self.assertEqual(
            rules[0]["responseModifications"]["applyHeadersToEnd"], True
        )
        self.assertEqual(
            rules[0]["responseModifications"]["headers"][0],
            {"key": "X-Test", "value": "preserve"},
        )
        self.assertEqual(
            rules[1]["responseModifications"]["headers"],
            [{"key": "X-Other", "value": "untouched"}],
        )
        self.assertEqual(self.settings, original)

    def test_duplicate_routing_headers_fail_closed(self):
        self.settings["responseRules"]["rules"][0]["responseModifications"][
            "headers"
        ].append({"key": "routing", "value": self.old})
        with self.assertRaisesRegex(ValueError, "duplicate routing headers"):
            build_settings_payload(self.settings, self.config, self.new)

    def test_missing_named_rule_fails_closed(self):
        config = Config(**{**self.config.__dict__, "response_rule_name": "Missing"})
        with self.assertRaisesRegex(ValueError, "was not found"):
            build_settings_payload(self.settings, config, self.new)

    def test_global_mode_preserves_unrelated_headers(self):
        config = make_config(self.tmpdir.name, target="global")
        settings = {
            "uuid": self.settings["uuid"],
            "customResponseHeaders": {"X-Test": "preserve", "Routing": self.old},
        }
        payload = build_settings_payload(settings, config, self.new)
        self.assertEqual(payload["customResponseHeaders"]["X-Test"], "preserve")
        self.assertEqual(payload["customResponseHeaders"]["routing"], self.new)
        self.assertNotIn("Routing", payload["customResponseHeaders"])


if __name__ == "__main__":
    unittest.main()
