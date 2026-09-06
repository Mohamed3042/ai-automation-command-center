"""`docs/openapi.v1.yaml` is the contract; the route table is the implementation.

The two are compared in both directions, so neither a route without a
specification entry nor an entry without a route can reach `main`. The document
is read with `tests/support/yamlmini.py` (standard library only) and, when
PyYAML and openapi-spec-validator are installed, also cross-checked against them.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import yamlmini  # noqa: E402  (path is prepared above)

from app import api  # noqa: E402

SPEC_PATH = ROOT / "docs" / "openapi.v1.yaml"
HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


def spec_document() -> dict:
    return yamlmini.load_path(SPEC_PATH)


def spec_operations(document: dict) -> dict:
    """`{path: {METHOD: operationId}}` exactly as the document declares it."""
    operations = {}
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS:
                operations.setdefault(path, {})[method.upper()] = operation.get("operationId")
    return operations


class OpenApiContractTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = spec_document()
        cls.spec = spec_operations(cls.document)
        cls.served = api.route_templates()

    def test_document_declares_openapi_31_and_the_release_version(self):
        from app import __version__

        self.assertEqual(self.document["openapi"], "3.1.0")
        self.assertEqual(str(self.document["info"]["version"]), __version__)
        self.assertIn("release-v{0}".format(__version__), (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_every_served_route_is_documented(self):
        missing = sorted(set(self.served) - set(self.spec))
        self.assertEqual(missing, [], "routes served with no OpenAPI path: {0}".format(missing))

    def test_every_documented_path_is_served(self):
        extra = sorted(set(self.spec) - set(self.served))
        self.assertEqual(extra, [], "OpenAPI paths with no route: {0}".format(extra))

    def test_methods_match_per_path(self):
        for path in sorted(set(self.spec) & set(self.served)):
            self.assertEqual(sorted(self.spec[path]), sorted(self.served[path]), "method mismatch on {0}".format(path))

    def test_operation_ids_match_the_route_table_and_are_unique(self):
        documented = [operation for methods in self.spec.values() for operation in methods.values()]
        self.assertEqual(len(documented), len(set(documented)), "duplicate operationId in the document")
        served = sorted(operation for methods in self.served.values() for operation in methods.values())
        self.assertEqual(sorted(documented), served)

    def test_public_routes_declare_empty_security_and_others_inherit_the_key(self):
        public = {route.template for route in api.ROUTES if route.auth == "public"}
        for path, item in self.document["paths"].items():
            for method, operation in item.items():
                if method not in HTTP_METHODS:
                    continue
                if path in public:
                    self.assertEqual(operation.get("security"), [], "{0} is public and must declare `security: []`".format(path))
                else:
                    self.assertNotEqual(operation.get("security"), [], "{0} is authenticated and must not opt out of security".format(path))

    def test_every_operation_documents_at_least_one_response(self):
        for path, methods in self.document["paths"].items():
            for method, operation in methods.items():
                if method in HTTP_METHODS:
                    self.assertTrue(operation.get("responses"), "{0} {1} documents no response".format(method, path))

    def test_every_local_ref_resolves(self):
        refs = []

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref" and isinstance(value, str):
                        refs.append(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.document)
        self.assertGreater(len(refs), 20)
        for ref in sorted(set(refs)):
            self.assertTrue(ref.startswith("#/"), "only local refs are used: {0}".format(ref))
            cursor = self.document
            for part in ref[2:].split("/"):
                self.assertIn(part, cursor, "unresolved $ref {0}".format(ref))
                cursor = cursor[part]

    def test_the_server_serves_the_same_bytes(self):
        self.assertEqual(api.SPEC_PATH, SPEC_PATH)
        self.assertTrue(api.DOCS_PAGE.exists(), "the vendored docs page is missing")
        self.assertIn(b"/api/v1/openapi.yaml", api.DOCS_PAGE.read_bytes())


class SpecToolingTestCase(unittest.TestCase):
    """Guards the standard-library YAML reader itself, when the tooling is present."""

    def test_yamlmini_agrees_with_pyyaml(self):
        try:
            import yaml
        except ImportError:  # pragma: no cover - base CI job has no dev tooling
            self.skipTest("PyYAML is not installed; the spec-tools CI job installs it")
        with open(str(SPEC_PATH), encoding="utf-8") as handle:
            self.assertEqual(spec_document(), yaml.safe_load(handle))

    def test_document_validates_against_the_openapi_31_schema(self):
        try:
            from openapi_spec_validator import validate
        except ImportError:  # pragma: no cover - base CI job has no dev tooling
            self.skipTest("openapi-spec-validator is not installed; the spec-tools CI job installs it")
        validate(spec_document())


if __name__ == "__main__":
    unittest.main()
