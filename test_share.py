import base64
import gc
import hashlib
import io
import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault(
    "SHARE_SIGNING_PRIVATE_KEY",
    base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii"),
)
os.environ.setdefault("RATE_LIMIT_INSPECT_PER_WINDOW", "2")

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import main


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SignedShareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = main.app.test_client()
        main.RATE_LIMITER = main.MemoryRateLimiter(
            main.RATE_LIMIT_WINDOW_SECONDS, main.RATE_LIMIT_MAX_CLIENTS
        )
        main.UPLOAD_CACHE.clear()

    def analyze(self):
        source = b"value\tgroup\n1\tA\n2\tA\n3\tA\n4\tA\n5\tA\n6\tA\n"
        return self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "sample.tsv"),
                "x_column": "value",
                "hue1": "group",
                "hue2": "",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )

    def test_result_and_context_are_bound_and_signed(self):
        response = self.analyze()
        self.assertEqual(response.status_code, 200)
        share = response.get_json()["share"]
        public_key = Ed25519PublicKey.from_public_bytes(main.SHARE_PUBLIC_KEY)
        public_key.verify(decode(share["signature"]), share["payload"].encode())
        public_key.verify(
            decode(share["context_signature"]), share["context_payload"].encode()
        )
        context = json.loads(share["context_payload"])
        expected_digest = base64.urlsafe_b64encode(
            hashlib.sha256(share["payload"].encode()).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(context["result_digest"], expected_digest)
        self.assertNotIn("sample.tsv", share["payload"])

    def test_modified_result_fails_verification(self):
        share = self.analyze().get_json()["share"]
        public_key = Ed25519PublicKey.from_public_bytes(main.SHARE_PUBLIC_KEY)
        with self.assertRaises(InvalidSignature):
            public_key.verify(
                decode(share["signature"]), (share["payload"] + " ").encode()
            )

    def test_small_groups_are_not_signed(self):
        source = b"value\tgroup\n1\tA\n2\tA\n3\tA\n4\tA\n"
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "small.tsv"),
                "x_column": "value",
                "hue1": "group",
                "hue2": "",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["share"])
        self.assertIn("n = 5", response.get_json()["share_blocked_reason"])

    def test_public_key_and_security_headers(self):
        response = self.client.get("/api/share-key")
        data = response.get_json()
        self.assertEqual(data["algorithm"], "Ed25519")
        self.assertEqual(data["key_id"], main.SHARE_KEY_ID)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_health_exposes_bounded_cache_and_analysis_metadata(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["upload_cache"]["max_bytes"], main.UPLOAD_CACHE_MAX_BYTES)
        self.assertEqual(result["upload_cache"]["ttl_seconds"], main.UPLOAD_CACHE_TTL_SECONDS)
        self.assertEqual(result["analysis"]["kde_max_sample_size"], main.KDE_MAX_SAMPLE_SIZE)
        self.assertEqual(result["analysis"]["state_scope"], "process_local")
        self.assertEqual(
            result["inspection"]["max_concurrent_per_worker"],
            main.MAX_CONCURRENT_INSPECTIONS,
        )

    def test_inspect_concurrency_limit_is_enforced(self):
        acquired = [
            main.INSPECT_SEMAPHORE.acquire(blocking=False)
            for _ in range(main.MAX_CONCURRENT_INSPECTIONS)
        ]
        self.assertTrue(all(acquired))
        try:
            response = self.client.post("/api/inspect", data={})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "5")
        finally:
            for _ in acquired:
                main.INSPECT_SEMAPHORE.release()

    def test_inspect_rate_limit_is_enforced(self):
        def inspect_request():
            return self.client.post(
                "/api/inspect",
                data={"file": (io.BytesIO(b"value\tgroup\n1\tA\n2\tA\n"), "rate.tsv")},
                content_type="multipart/form-data",
                environ_base={"REMOTE_ADDR": "198.51.100.42"},
            )

        self.assertEqual(inspect_request().status_code, 200)
        second = inspect_request()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.headers["RateLimit-Remaining"], "0")
        limited = inspect_request()
        self.assertEqual(limited.status_code, 429)
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)

    def test_analysis_concurrency_limit_is_enforced(self):
        acquired = [
            main.ANALYZE_SEMAPHORE.acquire(blocking=False)
            for _ in range(main.MAX_CONCURRENT_ANALYSES)
        ]
        self.assertTrue(all(acquired))
        try:
            response = self.analyze()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "5")
        finally:
            for _ in acquired:
                main.ANALYZE_SEMAPHORE.release()

    def test_cloudflare_client_ip_requires_explicit_trust(self):
        original = main.TRUST_CF_CONNECTING_IP
        try:
            with main.app.test_request_context(
                "/",
                headers={"CF-Connecting-IP": "203.0.113.25"},
                environ_overrides={"REMOTE_ADDR": "172.17.0.1"},
            ):
                main.TRUST_CF_CONNECTING_IP = False
                direct_identity = main.client_identity()
                main.TRUST_CF_CONNECTING_IP = True
                cloudflare_identity = main.client_identity()
            self.assertNotEqual(direct_identity, cloudflare_identity)

            with main.app.test_request_context(
                "/",
                headers={"CF-Connecting-IP": "203.0.113.25, 198.51.100.1"},
                environ_overrides={"REMOTE_ADDR": "172.17.0.1"},
            ):
                self.assertEqual(main.client_identity(), direct_identity)
        finally:
            main.TRUST_CF_CONNECTING_IP = original

    def test_nested_filter_semantics_remain_correct(self):
        frame = main.pd.DataFrame(
            {"value": [1, 2, 3, 4, 5], "group": ["A", "B", "A", "B", "A"]}
        )
        tree = {
            "type": "group",
            "logic": "AND",
            "children": [
                {"type": "condition", "column": "value", "operator": ">", "value": 2},
                {
                    "type": "group",
                    "logic": "OR",
                    "children": [
                        {"type": "condition", "column": "group", "operator": "==", "value": "A"},
                        {"type": "condition", "column": "value", "operator": "<", "value": 5},
                    ],
                },
            ],
        }
        self.assertEqual(list(frame.index[main.filter_mask(frame, tree)]), [2, 3, 4])

    def test_wide_filter_tree_keeps_only_two_masks_live(self):
        class TrackingMask:
            active = 0
            peak = 0

            def __init__(self):
                type(self).active += 1
                type(self).peak = max(type(self).peak, type(self).active)

            def __iand__(self, other):
                return self

            def __ior__(self, other):
                return self

            def __and__(self, other):
                return self

            def __or__(self, other):
                return self

            def __del__(self):
                type(self).active -= 1

        tree = {
            "type": "group",
            "logic": "AND",
            "children": [
                {"type": "condition", "column": "value", "operator": ">", "value": 0}
                for _ in range(99)
            ],
        }
        with patch.object(main, "condition_mask", side_effect=lambda *_: TrackingMask()):
            result = main.filter_mask(main.pd.DataFrame({"value": [1]}), tree)
        self.assertLessEqual(TrackingMask.peak, 2)
        del result
        gc.collect()
        self.assertEqual(TrackingMask.active, 0)

    def test_service_worker_uses_explicit_cache_allowlist(self):
        response = self.client.get("/service-worker.js")
        self.assertEqual(response.status_code, 200)
        source = response.get_data(as_text=True)
        self.assertNotIn("__APP_VERSION__", source)
        self.assertIn("STATIC_PATHS.has(url.pathname)", source)
        self.assertIn('event.request.mode === "navigate"', source)

    def test_german_decimal_inspection_and_cached_analysis(self):
        source = (
            "wert;gruppe\n"
            "1,5;A\n"
            "2,5;A\n"
            "ungültig;A\n"
            "3,5;A\n"
            "4,5;A\n"
        ).encode("utf-8")
        remote = {"REMOTE_ADDR": "198.51.100.10"}
        inspected = self.client.post(
            "/api/inspect",
            data={"file": (io.BytesIO(source), "de.csv")},
            content_type="multipart/form-data",
            environ_base=remote,
        )
        self.assertEqual(inspected.status_code, 200, inspected.get_data(as_text=True))
        result = inspected.get_json()
        self.assertEqual(result["parse_options"]["delimiter"], ";")
        self.assertEqual(result["parse_options"]["decimal_separator"], ",")
        value_column = next(column for column in result["columns"] if column["name"] == "wert")
        self.assertTrue(value_column["numeric"])
        self.assertEqual(value_column["invalid_numeric_count"], 1)
        self.assertTrue(value_column["recommended_x"])
        self.assertTrue(value_column["top_values"])

        analyzed = self.client.post(
            "/api/analyze",
            data={
                "upload_token": result["upload_token"],
                "x_column": "wert",
                "hue1": "gruppe",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            environ_base=remote,
        )
        self.assertEqual(analyzed.status_code, 200, analyzed.get_data(as_text=True))
        payload = analyzed.get_json()
        self.assertEqual(payload["upload_source"], "cache")
        self.assertEqual(payload["statistics"][0]["count"], 4)
        self.assertEqual(payload["exclusions"]["x_missing_or_invalid"], 1)

    def test_one_column_comma_decimals_are_not_mistaken_for_csv_delimiter(self):
        source = "wert\n1,5\n2,5\n3,5\n".encode("utf-8")
        response = self.client.post(
            "/api/inspect",
            data={"file": (io.BytesIO(source), "one-column.csv")},
            content_type="multipart/form-data",
            environ_base={"REMOTE_ADDR": "198.51.100.13"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(len(result["columns"]), 1)
        self.assertTrue(result["columns"][0]["numeric"])
        self.assertEqual(result["parse_options"]["decimal_separator"], ",")
        self.assertEqual(result["preview"][0]["wert"], 1.5)

    def test_manual_import_options_and_thousands_separator(self):
        source = "wert;gruppe\n1.234,5;A\n2.345,5;A\n".encode("utf-8")
        response = self.client.post(
            "/api/inspect",
            data={
                "file": (io.BytesIO(source), "manual.csv"),
                "parse_encoding": "utf-8",
                "parse_delimiter": ";",
                "decimal_separator": ",",
                "thousands_separator": ".",
            },
            content_type="multipart/form-data",
            environ_base={"REMOTE_ADDR": "198.51.100.11"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(result["preview"][0]["wert"], 1234.5)
        self.assertEqual(result["parse_options"]["sources"]["decimal_separator"], "manual")
        self.assertEqual(result["parse_options"]["thousands_separator"], ".")

    def test_inspect_token_can_reparse_without_second_upload(self):
        source = "wert;gruppe\n1.234,5;A\n2.345,5;B\n".encode("utf-8")
        remote = {"REMOTE_ADDR": "198.51.100.12"}
        first = self.client.post(
            "/api/inspect",
            data={"file": (io.BytesIO(source), "reparse.csv")},
            content_type="multipart/form-data",
            environ_base=remote,
        )
        self.assertEqual(first.status_code, 200)
        token = first.get_json()["upload_token"]
        second = self.client.post(
            "/api/inspect",
            data={
                "upload_token": token,
                "delimiter": ";",
                "decimal": ",",
                "thousands": ".",
            },
            environ_base=remote,
        )
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        result = second.get_json()
        self.assertEqual(result["upload_token"], token)
        self.assertEqual(result["preview"][1]["wert"], 2345.5)
        self.assertEqual(result["parse_options"]["sources"]["delimiter"], "manual")

    def test_continuous_mode_statistics_histogram_and_bandwidth(self):
        source = b"value\tgroup\n1\tA\n2\tA\n3\tA\n4\tA\n5\tA\n"
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "continuous.tsv"),
                "x_column": "value",
                "hue1": "group",
                "bandwidth": "silverman",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        row = result["statistics"][0]
        curve = result["curves"][0]
        self.assertIsNone(row["mode"])
        self.assertIsNotNone(row["density_mode"])
        self.assertEqual(row["q1"], 2.0)
        self.assertEqual(row["q3"], 4.0)
        self.assertEqual(row["iqr"], 2.0)
        self.assertEqual(row["mad"], 1.0)
        self.assertLess(row["ci_low"], row["mean"])
        self.assertGreater(row["ci_high"], row["mean"])
        self.assertEqual(curve["references"]["mean"], 3.0)
        self.assertIn("x", curve["histogram"])
        self.assertIn("y", curve["histogram"])
        self.assertEqual(len(curve["rug"]), 5)
        self.assertEqual(result["methodology"]["kde"]["bandwidth"], "silverman")
        self.assertTrue(row["modality_metadata"]["heuristic"])

    def test_nan_and_infinity_are_excluded_and_reported(self):
        source = b"value,group\n1,A\n2,A\ninf,A\nNaN,A\n3,A\n"
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "nonfinite.csv"),
                "x_column": "value",
                "hue1": "group",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(result["exclusions"]["x_missing_or_invalid"], 1)
        self.assertEqual(result["exclusions"]["x_non_finite"], 1)
        self.assertEqual(result["statistics"][0]["count"], 3)
        self.assertNotIn("NaN", response.get_data(as_text=True))
        self.assertNotIn("Infinity", response.get_data(as_text=True))

    def test_upload_token_is_bound_to_client_identity(self):
        inspected = self.client.post(
            "/api/inspect",
            data={"file": (io.BytesIO(b"value\n1\n2\n3\n"), "token.tsv")},
            content_type="multipart/form-data",
            environ_base={"REMOTE_ADDR": "198.51.100.20"},
        )
        token = inspected.get_json()["upload_token"]
        response = self.client.post(
            "/api/analyze",
            data={
                "upload_token": token,
                "x_column": "value",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            environ_base={"REMOTE_ADDR": "198.51.100.21"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("anderen Sitzung", response.get_json()["error"])

    def test_duplicate_hues_and_invalid_bandwidth_are_rejected(self):
        source = b"value\tgroup\n1\tA\n2\tA\n3\tB\n4\tB\n"
        common = {
            "x_column": "value",
            "hue1": "group",
            "hue2": "group",
            "filter_tree": json.dumps(
                {"type": "group", "logic": "AND", "children": []}
            ),
        }
        duplicated = self.client.post(
            "/api/analyze",
            data={"file": (io.BytesIO(source), "duplicate.tsv"), **common},
            content_type="multipart/form-data",
        )
        self.assertEqual(duplicated.status_code, 400)
        self.assertIn("nicht zweimal", duplicated.get_json()["error"])

        invalid_bandwidth = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "bandwidth.tsv"),
                "x_column": "value",
                "bandwidth": "0",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid_bandwidth.status_code, 400)
        self.assertIn("Bandbreitenfaktor", invalid_bandwidth.get_json()["error"])

    def test_kde_sampling_is_deterministic_and_statistics_remain_exact(self):
        source = b"value\n1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n"
        original = main.KDE_MAX_SAMPLE_SIZE
        main.KDE_MAX_SAMPLE_SIZE = 3
        try:
            def analyze_sample():
                return self.client.post(
                    "/api/analyze",
                    data={
                        "file": (io.BytesIO(source), "sample.tsv"),
                        "x_column": "value",
                        "bandwidth": "0.5",
                        "filter_tree": json.dumps(
                            {"type": "group", "logic": "AND", "children": []}
                        ),
                    },
                    content_type="multipart/form-data",
                ).get_json()

            first = analyze_sample()
            second = analyze_sample()
            self.assertEqual(first["curves"][0]["kde_sample_size"], 3)
            self.assertTrue(first["curves"][0]["kde_sampled"])
            self.assertEqual(first["curves"][0]["x"], second["curves"][0]["x"])
            self.assertEqual(first["curves"][0]["y"], second["curves"][0]["y"])
            self.assertEqual(first["statistics"][0]["mean"], 5.5)
            self.assertEqual(first["statistics"][0]["count"], 10)
        finally:
            main.KDE_MAX_SAMPLE_SIZE = original

    def test_share_expiry_reproducibility_and_column_config_are_signed(self):
        source = b"value\tgroup\n1\tA\n2\tA\n3\tA\n4\tA\n5\tA\n"
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "configured.tsv"),
                "x_column": "value",
                "hue1": "group",
                "bandwidth": "0.75",
                "share_expiry_days": "7",
                "column_config": json.dumps(
                    {"value": {"alias": "Preis", "unit": "EUR"}}
                ),
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(result["x_label"], "value")
        self.assertEqual(result["display_x_label"], "Preis [EUR]")
        share = result["share"]
        payload = json.loads(share["payload"])
        created = datetime.fromisoformat(payload["created_at"])
        expires = datetime.fromisoformat(payload["expires_at"])
        self.assertEqual((expires - created).days, 7)
        self.assertEqual(payload["reproducibility"]["bandwidth"], 0.75)
        self.assertEqual(
            payload["reproducibility"]["column_config"]["value"]["alias"], "Preis"
        )
        self.assertIn("parse_options", payload["reproducibility"])
        public_key = Ed25519PublicKey.from_public_bytes(main.SHARE_PUBLIC_KEY)
        public_key.verify(decode(share["signature"]), share["payload"].encode())

    def test_historical_keyring_is_exposed_without_breaking_current_fields(self):
        encoded = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode()
        with patch.dict(os.environ, {"SHARE_PUBLIC_KEYRING": json.dumps({"legacy": encoded})}):
            ring = main.load_share_public_keyring()
        self.assertEqual(ring["legacy"], bytes(reversed(range(32))))
        original = main.SHARE_PUBLIC_KEYRING
        main.SHARE_PUBLIC_KEYRING = ring
        try:
            response = self.client.get("/api/share-key")
            result = response.get_json()
            self.assertEqual(result["key_id"], main.SHARE_KEY_ID)
            self.assertEqual(result["public_key"], main.base64url_encode(main.SHARE_PUBLIC_KEY))
            self.assertIn("legacy", result["public_keys"])
            self.assertTrue(any(item["current"] for item in result["keys"]))
        finally:
            main.SHARE_PUBLIC_KEYRING = original

    def test_estimate_and_top_n_other_bucket_match_analysis(self):
        lines = ["value\tgroup"]
        value = 1
        for group, count in (("A", 5), ("B", 4), ("C", 3), ("D", 2)):
            for _ in range(count):
                lines.append(f"{value}\t{group}")
                value += 1
        source = ("\n".join(lines) + "\n").encode()
        remote = {"REMOTE_ADDR": "198.51.100.30"}
        inspected = self.client.post(
            "/api/inspect",
            data={"file": (io.BytesIO(source), "segments.tsv")},
            content_type="multipart/form-data",
            environ_base=remote,
        ).get_json()
        common = {
            "upload_token": inspected["upload_token"],
            "x_column": "value",
            "hue1": "group",
            "segment_top_n": json.dumps({"group": 2}),
            "filter_tree": json.dumps(
                {"type": "group", "logic": "AND", "children": []}
            ),
        }
        estimate = self.client.post("/api/estimate", data=common, environ_base=remote)
        self.assertEqual(estimate.status_code, 200, estimate.get_data(as_text=True))
        estimate_result = estimate.get_json()
        self.assertEqual(estimate_result["curve_count"], 3)
        self.assertEqual(estimate_result["cardinalities"]["group"]["original"], 4)
        self.assertEqual(estimate_result["cardinalities"]["group"]["effective"], 3)
        self.assertTrue(any(item["label"] == "Sonstige" for item in estimate_result["group_sizes"]))

        analyzed = self.client.post("/api/analyze", data=common, environ_base=remote)
        self.assertEqual(analyzed.status_code, 200, analyzed.get_data(as_text=True))
        self.assertEqual(len(analyzed.get_json()["curves"]), estimate_result["curve_count"])

    def test_share_gate_includes_original_singleton_groups(self):
        source = b"value\tgroup\n1\tA\n2\tA\n3\tA\n4\tA\n5\tA\n6\tB\n"
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "singleton.tsv"),
                "x_column": "value",
                "hue1": "group",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(len(result["curves"]), 1)
        self.assertEqual(result["source_rows"], 6)
        self.assertEqual(result["plotted_rows"], 5)
        self.assertEqual(result["omitted_small_group_count"], 1)
        self.assertEqual(result["omitted_small_group_rows"], 1)
        self.assertEqual(result["exclusions"]["omitted_small_group_rows"], 1)
        self.assertEqual(result["reproducibility"]["omitted_small_group_rows"], 1)
        self.assertIsNone(result["share"])
        self.assertIn("ursprüngliche", result["share_blocked_reason"])

    def test_top_n_other_bucket_never_collides_with_real_category(self):
        lines = ["value\tgroup"]
        value = 1
        for group, count in (("Sonstige", 7), ("A", 6), ("B", 5)):
            for _ in range(count):
                lines.append(f"{value}\t{group}")
                value += 1
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(("\n".join(lines) + "\n").encode()), "other.tsv"),
                "x_column": "value",
                "hue1": "group",
                "segment_top_n": "1",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        labels = [curve["label"] for curve in result["curves"]]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertTrue(any(label.startswith("Sonstige (n=") for label in labels))
        self.assertTrue(any("Sonstige (gebündelt)" in label for label in labels))
        segment_values = [curve["segment_values"]["group"] for curve in result["curves"]]
        self.assertIn("Sonstige", segment_values)
        self.assertTrue(any(isinstance(value, dict) and value.get("kind") == "other" for value in segment_values))
        self.assertIsNotNone(result["share"])

    def test_structured_segment_values_prevent_plus_label_collisions(self):
        lines = ["value\thue1\thue2"]
        for value in range(1, 6):
            lines.append(f"{value}\tA + B\tC")
        for value in range(6, 11):
            lines.append(f"{value}\tA\tB + C")
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(("\n".join(lines) + "\n").encode()), "labels.tsv"),
                "x_column": "value",
                "hue1": "hue1",
                "hue2": "hue2",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        curves = response.get_json()["curves"]
        self.assertEqual(len({curve["label"] for curve in curves}), 2)
        self.assertEqual(len({curve["segment_key"] for curve in curves}), 2)
        combinations = {
            (curve["segment_values"]["hue1"], curve["segment_values"]["hue2"])
            for curve in curves
        }
        self.assertEqual(combinations, {("A + B", "C"), ("A", "B + C")})

    def test_non_finite_hue_values_have_distinct_canonical_segments(self):
        source = b"value\tgroup\n1\tinf\n2\tinf\n3\t-inf\n4\t-inf\n5\t0\n6\t0\n"
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "hue-infinity.tsv"),
                "x_column": "value",
                "hue1": "group",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        curves = response.get_json()["curves"]
        self.assertEqual(len(curves), 3)
        self.assertEqual(len({curve["label"] for curve in curves}), 3)
        self.assertEqual(len({curve["segment_key"] for curve in curves}), 3)
        tagged = [
            curve["segment_values"]["group"]
            for curve in curves
            if isinstance(curve["segment_values"]["group"], dict)
        ]
        self.assertEqual(
            {value["value"] for value in tagged}, {"+Infinity", "-Infinity"}
        )

    def test_share_payload_uses_deepcopy_without_rug_observations(self):
        result = self.analyze().get_json()
        self.assertTrue(result["curves"][0]["rug"])
        signed = json.loads(result["share"]["payload"])
        self.assertNotIn("rug", signed["result"]["curves"][0])
        self.assertIn("rug", result["curves"][0])

    def test_signed_column_config_only_contains_used_analysis_columns(self):
        source = (
            b"value\tgroup\tsecret\n"
            b"1\tA\tpatient-1\n2\tA\tpatient-2\n3\tA\tpatient-3\n"
            b"4\tA\tpatient-4\n5\tA\tpatient-5\n"
        )
        column_config = {
            "value": {"alias": "Messwert", "unit": "kg"},
            "group": {"alias": "Kohorte", "unit": ""},
            "secret": {"alias": "Patientencode intern", "unit": ""},
        }
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "private-config.tsv"),
                "x_column": "value",
                "hue1": "group",
                "column_config": json.dumps(column_config),
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        local = response.get_json()
        self.assertIn("secret", local["column_config"])
        signed = json.loads(local["share"]["payload"])
        expected = {"value", "group"}
        self.assertEqual(set(signed["result"]["column_config"]), expected)
        self.assertEqual(set(signed["reproducibility"]["column_config"]), expected)
        self.assertNotIn("Patientencode intern", local["share"]["payload"])

    def test_oversized_uncompressed_share_is_blocked_server_side(self):
        original = main.MAX_SHARED_JSON_BYTES
        main.MAX_SHARED_JSON_BYTES = 500
        try:
            result = self.analyze().get_json()
        finally:
            main.MAX_SHARED_JSON_BYTES = original
        self.assertIsNone(result["share"])
        self.assertIn("zu groß", result["share_blocked_reason"])

    def test_bandwidth_number_is_scott_multiplier(self):
        series = main.pd.Series(main.np.linspace(0.0, 10.0, 100))
        scott = main.curve_for(series, "scott")["kde_bandwidth_factor"]
        one = main.curve_for(series, 1.0)["kde_bandwidth_factor"]
        two = main.curve_for(series, 2.0)["kde_bandwidth_factor"]
        self.assertAlmostEqual(one, scott, places=12)
        self.assertAlmostEqual(two, scott * 2, places=12)

    def test_tied_repeated_modes_are_reported_as_ambiguous(self):
        source = b"value\n1\n1\n2\n2\n3\n"
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "tied-mode.tsv"),
                "x_column": "value",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        row = response.get_json()["statistics"][0]
        self.assertIsNone(row["mode"])
        self.assertTrue(row["mode_tied"])
        self.assertEqual(row["mode_values"], [1.0, 2.0])
        self.assertEqual(row["mode_count"], 2)

    def test_curve_precision_preserves_large_and_tiny_scales(self):
        large = main.curve_for(main.pd.Series(main.np.linspace(0.0, 1e9, 100)))
        self.assertTrue(any(value > 0 for value in large["y"]))
        self.assertTrue(any(value > 0 for value in large["histogram"]["y"]))
        tiny = main.curve_for(main.pd.Series(main.np.linspace(1e-12, 1e-10, 100)))
        self.assertTrue(any(value != 0 for value in tiny["x"]))
        self.assertTrue(all(value > 0 for value in tiny["rug"]))

    def test_extreme_finite_span_gets_clear_validation_error(self):
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(b"value\n-1e308\n1e308\n"), "extreme.tsv"),
                "x_column": "value",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Zahlenbereich", response.get_json()["error"])

    def test_subnormal_nonzero_span_gets_clear_validation_error(self):
        source = b"value\n0\n5e-324\n0\n5e-324\n0\n5e-324\n"
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "subnormal.tsv"),
                "x_column": "value",
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("zu klein", response.get_json()["error"])

    def test_signed_text_contract_limits_are_validated_early(self):
        long_column = "x" * 501
        response = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(f"{long_column}\n1\n2\n".encode()), "long.tsv"),
                "x_column": long_column,
                "filter_tree": json.dumps(
                    {"type": "group", "logic": "AND", "children": []}
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("500 Zeichen", response.get_json()["error"])

        source = b"value\tgroup\n1\tA\n2\tA\n"
        too_long_filter = self.client.post(
            "/api/analyze",
            data={
                "file": (io.BytesIO(source), "filter.tsv"),
                "x_column": "value",
                "filter_tree": json.dumps(
                    {
                        "type": "condition",
                        "column": "group",
                        "operator": "==",
                        "value": "a" * 10001,
                    }
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(too_long_filter.status_code, 400)
        self.assertIn("Filterzusammenfassung", too_long_filter.get_json()["error"])

    def test_high_cardinality_estimate_is_bounded_and_analyze_fails_early(self):
        lines = ["value\tid"] + [f"{index}\tid-{index}" for index in range(1, 251)]
        source = ("\n".join(lines) + "\n").encode()
        common = {
            "x_column": "value",
            "hue1": "id",
            "filter_tree": json.dumps(
                {"type": "group", "logic": "AND", "children": []}
            ),
        }
        estimate = self.client.post(
            "/api/estimate",
            data={"file": (io.BytesIO(source), "ids.tsv"), **common},
            content_type="multipart/form-data",
        )
        self.assertEqual(estimate.status_code, 200, estimate.get_data(as_text=True))
        estimated = estimate.get_json()
        self.assertEqual(estimated["observed_group_count"], 250)
        self.assertEqual(estimated["curve_count"], 0)
        self.assertEqual(estimated["plotted_rows"], 0)
        self.assertEqual(estimated["omitted_small_group_count"], 250)
        self.assertEqual(estimated["omitted_small_group_rows"], 250)
        self.assertEqual(len(estimated["group_sizes"]), 100)
        self.assertTrue(estimated["group_sizes_truncated"])
        self.assertTrue(estimated["exceeds_curve_limit"])

        analyzed = self.client.post(
            "/api/analyze",
            data={"file": (io.BytesIO(source), "ids.tsv"), **common},
            content_type="multipart/form-data",
        )
        self.assertEqual(analyzed.status_code, 400)
        self.assertIn("250 Gruppen", analyzed.get_json()["error"])

    def test_estimate_uses_analysis_concurrency_semaphore(self):
        acquired = [
            main.ANALYZE_SEMAPHORE.acquire(blocking=False)
            for _ in range(main.MAX_CONCURRENT_ANALYSES)
        ]
        self.assertTrue(all(acquired))
        try:
            response = self.client.post("/api/estimate", data={})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "5")
        finally:
            for _ in acquired:
                main.ANALYZE_SEMAPHORE.release()


if __name__ == "__main__":
    unittest.main()
