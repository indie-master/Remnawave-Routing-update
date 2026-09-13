from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import time
import urllib3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

ROUTING_HEADER = "routing"
DEEPLINK_RE = re.compile(r"^happ://routing/(?:add|onadd)/([A-Za-z0-9+/=]+)$")
DEFAULT_GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/indie-master/happ-routing/"
    "main/HAPP/DEFAULT.DEEPLINK"
)


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def env_profile_renames(
    name: str = "ALLOWED_PROFILE_RENAMES",
) -> frozenset[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    value = os.environ.get(name, "")
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        if item.count(":") != 1:
            raise ValueError(f"{name} entries must use old-name:new-name")
        old_name, new_name = (part.strip() for part in item.split(":", 1))
        if not old_name or not new_name or old_name == new_name:
            raise ValueError(f"Invalid {name} entry: {item!r}")
        result.add((old_name, new_name))
    return frozenset(result)


@dataclass(frozen=True)
class SquadConfig:
    uuid: str
    url: str


@dataclass(frozen=True)
class Config:
    remna_base_url: str
    remna_token: str
    github_raw_url: str
    update_target: str
    response_rule_name: str
    check_interval: int
    cron_schedule: str
    ssl_verify: bool
    dry_run: bool
    validate_geo_urls: bool
    allow_profile_rename: bool
    allowed_profile_renames: frozenset[tuple[str, str]]
    backup_dir: Path
    allowed_geo_hosts: frozenset[str]
    request_timeout: int
    squads: tuple[SquadConfig, ...]

    @classmethod
    def from_env(cls) -> "Config":
        base_url = os.environ["REMNA_BASE_URL"].rstrip("/")
        token = os.environ["REMNA_TOKEN"].strip()
        if not token:
            raise ValueError("REMNA_TOKEN must not be empty")

        target = os.environ.get("UPDATE_TARGET", "response-rule").strip().lower()
        if target not in {"response-rule", "global"}:
            raise ValueError("UPDATE_TARGET must be response-rule or global")

        interval = int(os.environ.get("CHECK_INTERVAL", "21600"))
        timeout = int(os.environ.get("REQUEST_TIMEOUT", "30"))
        if interval < 60:
            raise ValueError("CHECK_INTERVAL must be at least 60 seconds")
        if timeout < 1:
            raise ValueError("REQUEST_TIMEOUT must be positive")

        allowed_hosts = frozenset(
            item.strip().lower()
            for item in os.environ.get(
                "ALLOWED_GEO_HOSTS",
                "cdn.jsdelivr.net,raw.githubusercontent.com,github.com",
            ).split(",")
            if item.strip()
        )
        if not allowed_hosts:
            raise ValueError("ALLOWED_GEO_HOSTS must not be empty")

        squads: list[SquadConfig] = []
        index = 1
        while True:
            uuid = os.environ.get(f"SQUAD_{index}_UUID", "").strip()
            url = os.environ.get(f"SQUAD_{index}_URL", "").strip()
            if not uuid and not url:
                break
            if not uuid or not url:
                raise ValueError(
                    f"SQUAD_{index}_UUID and SQUAD_{index}_URL must be set together"
                )
            squads.append(SquadConfig(uuid=uuid, url=url))
            index += 1

        response_rule_name = os.environ.get("RESPONSE_RULE_NAME", "Happ").strip()
        if target == "response-rule" and not response_rule_name:
            raise ValueError("RESPONSE_RULE_NAME must not be empty")

        return cls(
            remna_base_url=base_url,
            remna_token=token,
            github_raw_url=os.environ.get(
                "GITHUB_RAW_URL", DEFAULT_GITHUB_RAW_URL
            ).strip(),
            update_target=target,
            response_rule_name=response_rule_name,
            check_interval=interval,
            cron_schedule=os.environ.get("CRON_SCHEDULE", "").strip(),
            ssl_verify=env_bool("REMNA_SSL_VERIFY", True),
            dry_run=env_bool("DRY_RUN", True),
            validate_geo_urls=env_bool("VALIDATE_GEO_URLS", True),
            allow_profile_rename=env_bool("ALLOW_PROFILE_RENAME", False),
            allowed_profile_renames=env_profile_renames(),
            backup_dir=Path(os.environ.get("BACKUP_DIR", "/data/backups")),
            allowed_geo_hosts=allowed_hosts,
            request_timeout=timeout,
            squads=tuple(squads),
        )


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


class RemnawaveClient:
    def __init__(self, config: Config, session: requests.Session | None = None):
        self.config = config
        self.session = session or make_session()
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {config.remna_token}",
        }
        if config.remna_base_url.startswith("http://"):
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self.headers["X-Forwarded-Proto"] = "https"
            self.headers["X-Forwarded-For"] = "127.0.0.1"

    def _unwrap(self, response: requests.Response) -> dict[str, Any]:
        response.raise_for_status()
        body = response.json()
        data = body.get("response", body)
        if not isinstance(data, dict):
            raise ValueError("Unexpected Remnawave API response")
        return data

    def get_settings(self) -> dict[str, Any]:
        response = self.session.get(
            f"{self.config.remna_base_url}/subscription-settings",
            headers=self.headers,
            timeout=self.config.request_timeout,
            verify=self.config.ssl_verify,
        )
        return self._unwrap(response)

    def patch_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.patch(
            f"{self.config.remna_base_url}/subscription-settings",
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=self.config.request_timeout,
            verify=self.config.ssl_verify,
        )
        return self._unwrap(response)

    def get_external_squad(self, squad_uuid: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.config.remna_base_url}/external-squads/{squad_uuid}",
            headers=self.headers,
            timeout=self.config.request_timeout,
            verify=self.config.ssl_verify,
        )
        return self._unwrap(response)

    def patch_external_squad(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.patch(
            f"{self.config.remna_base_url}/external-squads",
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=self.config.request_timeout,
            verify=self.config.ssl_verify,
        )
        return self._unwrap(response)

    def fetch_text(self, url: str) -> str:
        response = self.session.get(url, timeout=self.config.request_timeout)
        response.raise_for_status()
        return response.text.strip()

    def check_url(self, url: str) -> None:
        response = self.session.head(
            url,
            allow_redirects=True,
            timeout=self.config.request_timeout,
        )
        if response.status_code in {403, 405}:
            response.close()
            response = self.session.get(
                url,
                headers={"Range": "bytes=0-0"},
                stream=True,
                timeout=self.config.request_timeout,
            )
        try:
            response.raise_for_status()
        finally:
            response.close()


def get_dict_header(
    headers: dict[str, Any] | None, name: str = ROUTING_HEADER
) -> str:
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return str(value or "").strip()
    return ""


def with_dict_header(headers: dict[str, Any] | None, value: str) -> dict[str, Any]:
    result = {
        key: item
        for key, item in (headers or {}).items()
        if str(key).lower() != ROUTING_HEADER
    }
    result[ROUTING_HEADER] = value
    return result


def get_list_header(
    headers: list[dict[str, Any]] | None, name: str = ROUTING_HEADER
) -> str:
    matches: list[str] = []
    for header in headers or []:
        if not isinstance(header, dict) or "key" not in header or "value" not in header:
            raise ValueError("Invalid header entry in Response Rule")
        if str(header["key"]).lower() == name.lower():
            matches.append(str(header["value"] or "").strip())
    if len(matches) > 1:
        raise ValueError("Response Rule contains duplicate routing headers")
    return matches[0] if matches else ""


def with_list_header(
    headers: list[dict[str, Any]] | None, value: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    routing_headers = 0
    for header in headers or []:
        if not isinstance(header, dict) or "key" not in header or "value" not in header:
            raise ValueError("Invalid header entry in Response Rule")
        preserved = copy.deepcopy(header)
        if str(header["key"]).lower() == ROUTING_HEADER:
            routing_headers += 1
            preserved["value"] = value
        result.append(preserved)
    if routing_headers > 1:
        raise ValueError("Response Rule contains duplicate routing headers")
    if routing_headers == 0:
        result.append({"key": ROUTING_HEADER, "value": value})
    return result


def find_response_rule(
    response_rules: dict[str, Any] | None, name: str
) -> dict[str, Any]:
    if not isinstance(response_rules, dict):
        raise ValueError("Remnawave responseRules is not configured")
    rules = response_rules.get("rules")
    if not isinstance(rules, list):
        raise ValueError("Remnawave responseRules.rules is not an array")
    matches = [
        rule
        for rule in rules
        if isinstance(rule, dict) and rule.get("name") == name
    ]
    if not matches:
        raise ValueError(f"Response Rule {name!r} was not found")
    if len(matches) > 1:
        raise ValueError(f"More than one Response Rule is named {name!r}")
    return matches[0]


def current_settings_routing(settings: dict[str, Any], config: Config) -> str:
    if config.update_target == "global":
        return get_dict_header(settings.get("customResponseHeaders"))
    rule = find_response_rule(settings.get("responseRules"), config.response_rule_name)
    modifications = rule.get("responseModifications") or {}
    if not isinstance(modifications, dict):
        raise ValueError("Response Rule responseModifications is not an object")
    return get_list_header(modifications.get("headers"))


def build_settings_payload(
    settings: dict[str, Any], config: Config, deeplink: str
) -> dict[str, Any]:
    uuid = settings.get("uuid")
    if not uuid:
        raise ValueError("Subscription settings UUID is missing")
    if config.update_target == "global":
        return {
            "uuid": uuid,
            "customResponseHeaders": with_dict_header(
                settings.get("customResponseHeaders"), deeplink
            ),
        }

    response_rules = copy.deepcopy(settings.get("responseRules"))
    rule = find_response_rule(response_rules, config.response_rule_name)
    modifications = rule.get("responseModifications") or {}
    if not isinstance(modifications, dict):
        raise ValueError("Response Rule responseModifications is not an object")
    modifications["headers"] = with_list_header(
        modifications.get("headers"), deeplink
    )
    rule["responseModifications"] = modifications
    return {"uuid": uuid, "responseRules": response_rules}


def decode_deeplink(
    deeplink: str, allowed_hosts: frozenset[str]
) -> dict[str, Any]:
    match = DEEPLINK_RE.fullmatch(deeplink.strip())
    if not match:
        raise ValueError("Unsupported or malformed Happ routing deeplink")
    encoded = match.group(1)
    encoded += "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(encoded, validate=True)
        profile = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Happ deeplink contains invalid Base64 or JSON") from exc
    if not isinstance(profile, dict):
        raise ValueError("Happ profile must be a JSON object")

    required = (
        "Name",
        "LastUpdated",
        "Geoipurl",
        "Geositeurl",
        "DirectSites",
        "DirectIp",
        "ProxySites",
        "BlockSites",
    )
    missing = [key for key in required if key not in profile]
    if missing:
        raise ValueError(f"Happ profile is missing: {', '.join(missing)}")
    if not str(profile["Name"]).strip():
        raise ValueError("Happ profile Name must not be empty")
    try:
        timestamp = int(profile["LastUpdated"])
    except (TypeError, ValueError) as exc:
        raise ValueError("LastUpdated must be a Unix timestamp") from exc
    if timestamp <= 0:
        raise ValueError("LastUpdated must be positive")

    for key in ("Geoipurl", "Geositeurl"):
        parsed = urlparse(str(profile[key]))
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or hostname not in allowed_hosts:
            raise ValueError(f"{key} has a disallowed URL")
    for key in ("DirectSites", "DirectIp", "ProxySites", "BlockSites"):
        if not isinstance(profile[key], list):
            raise ValueError(f"{key} must be an array")
    return profile


def should_update(
    current_deeplink: str,
    candidate_deeplink: str,
    candidate: dict[str, Any],
    allow_profile_rename: bool,
    allowed_hosts: frozenset[str],
    allowed_profile_renames: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[bool, str]:
    if current_deeplink.strip() == candidate_deeplink.strip():
        return False, "routing is already current"
    if not current_deeplink.strip():
        return True, "routing header is empty"

    try:
        current = decode_deeplink(current_deeplink, allowed_hosts)
    except ValueError:
        return True, "current routing header is invalid and will be repaired"

    rename = (str(current["Name"]), str(candidate["Name"]))
    if (
        current["Name"] != candidate["Name"]
        and not allow_profile_rename
        and rename not in allowed_profile_renames
    ):
        raise ValueError(
            f"Refusing profile rename from {current['Name']!r} "
            f"to {candidate['Name']!r}"
        )
    if int(candidate["LastUpdated"]) <= int(current["LastUpdated"]):
        return False, "candidate LastUpdated is not newer"
    return True, "newer validated routing profile is available"


def backup_json(data: dict[str, Any], config: Config, label: str) -> Path:
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label)
    path = config.backup_dir / f"{safe_label}-{timestamp}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return path


def validate_geo_files(
    client: RemnawaveClient, profile: dict[str, Any]
) -> None:
    for key in ("Geoipurl", "Geositeurl"):
        client.check_url(str(profile[key]))
        log.info("Validated %s", profile[key])


def update_subscription_settings(
    client: RemnawaveClient,
    config: Config,
    deeplink: str,
    profile: dict[str, Any],
) -> None:
    settings = client.get_settings()
    current = current_settings_routing(settings, config)
    update, reason = should_update(
        current,
        deeplink,
        profile,
        config.allow_profile_rename,
        config.allowed_geo_hosts,
        config.allowed_profile_renames,
    )
    if not update:
        log.info("Subscription settings: %s", reason)
        return
    log.info("Subscription settings: %s", reason)
    payload = build_settings_payload(settings, config, deeplink)
    if config.dry_run:
        log.warning("DRY_RUN: subscription settings were not changed")
        return

    backup_path = backup_json(settings, config, "subscription-settings")
    log.info("Backup written to %s", backup_path)
    client.patch_settings(payload)
    verified = client.get_settings()
    if current_settings_routing(verified, config) != deeplink:
        raise RuntimeError("Remnawave verification failed after PATCH")
    log.info("Subscription routing header updated and verified")


def update_squad(
    client: RemnawaveClient,
    config: Config,
    squad: SquadConfig,
    deeplink: str,
    profile: dict[str, Any],
) -> None:
    data = client.get_external_squad(squad.uuid)
    headers = data.get("responseHeadersAdd") or {}
    current = get_dict_header(headers)
    update, reason = should_update(
        current,
        deeplink,
        profile,
        config.allow_profile_rename,
        config.allowed_geo_hosts,
        config.allowed_profile_renames,
    )
    if not update:
        log.info("Squad %s: %s", squad.uuid, reason)
        return
    if config.dry_run:
        log.warning("DRY_RUN: squad %s was not changed (%s)", squad.uuid, reason)
        return

    backup_path = backup_json(data, config, f"squad-{squad.uuid}")
    log.info("Squad %s backup written to %s", squad.uuid, backup_path)
    original_remove = data.get("responseHeadersRemove") or []
    remove = [
        str(item)
        for item in original_remove
        if str(item).lower() != ROUTING_HEADER
    ]
    payload: dict[str, Any] = {
        "uuid": squad.uuid,
        "responseHeadersAdd": with_dict_header(headers, deeplink),
    }
    if remove != original_remove:
        payload["responseHeadersRemove"] = remove
    client.patch_external_squad(payload)
    verified = client.get_external_squad(squad.uuid)
    if get_dict_header(verified.get("responseHeadersAdd")) != deeplink:
        raise RuntimeError(f"Squad {squad.uuid} verification failed after PATCH")
    log.info("Squad %s routing header updated and verified", squad.uuid)


def run_cycle(client: RemnawaveClient, config: Config) -> None:
    try:
        deeplink = client.fetch_text(config.github_raw_url)
        profile = decode_deeplink(deeplink, config.allowed_geo_hosts)
        log.info(
            "Fetched validated profile %s, LastUpdated=%s",
            profile["Name"],
            profile["LastUpdated"],
        )
        if config.validate_geo_urls:
            validate_geo_files(client, profile)
        update_subscription_settings(client, config, deeplink, profile)
    except Exception:
        log.exception("Subscription settings update cycle failed")

    for squad in config.squads:
        try:
            deeplink = client.fetch_text(squad.url)
            profile = decode_deeplink(deeplink, config.allowed_geo_hosts)
            if config.validate_geo_urls:
                validate_geo_files(client, profile)
            update_squad(client, config, squad, deeplink, profile)
        except Exception:
            log.exception("Squad %s update cycle failed", squad.uuid)


def main() -> None:
    config = Config.from_env()
    client = RemnawaveClient(config)
    log.info("Starting Remnawave routing updater")
    log.info("Target: %s", config.update_target)
    if config.update_target == "response-rule":
        log.info("Response Rule: %s", config.response_rule_name)
    log.info("Source: %s", config.github_raw_url)
    log.info("DRY_RUN: %s", config.dry_run)

    if config.cron_schedule:
        from croniter import croniter

        if not croniter.is_valid(config.cron_schedule):
            raise SystemExit(f"Invalid CRON_SCHEDULE: {config.cron_schedule!r}")
        run_cycle(client, config)
        schedule = croniter(config.cron_schedule, datetime.now())
        while True:
            next_run = schedule.get_next(datetime)
            delay = (next_run - datetime.now()).total_seconds()
            if delay > 0:
                log.info(
                    "Next run at %s (in %ds)",
                    next_run.isoformat(sep=" ", timespec="seconds"),
                    int(delay),
                )
                time.sleep(delay)
            run_cycle(client, config)
    else:
        while True:
            run_cycle(client, config)
            time.sleep(config.check_interval)


if __name__ == "__main__":
    main()
