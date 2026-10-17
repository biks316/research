"""Checks that the app and its Pydantic models match openapi.yaml (the source of truth)."""

from pathlib import Path

import pytest
import yaml

from app.schemas import COMPONENT_MODELS

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text())
COMPONENTS = SPEC["components"]["schemas"]
CONSTRAINTS = ("minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems")
HTTP_METHODS = {"get", "put", "post", "delete", "patch"}


def _resolve(schema: dict, defs: dict) -> dict:
    if "$ref" in schema:
        return defs[schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _ref_name(schema: dict) -> str | None:
    ref = schema.get("$ref")
    return ref.rsplit("/", 1)[-1] if ref else None


def _spec_prop(prop: dict) -> tuple[dict, bool]:
    """Return (non-null schema, nullable) for a spec property."""
    if "oneOf" in prop:
        variants = [v for v in prop["oneOf"] if v.get("type") != "null"]
        return variants[0], len(variants) < len(prop["oneOf"])
    types = prop.get("type")
    if isinstance(types, list):
        non_null = [t for t in types if t != "null"]
        return {**prop, "type": non_null[0]}, "null" in types
    return prop, False


def _model_prop(prop: dict) -> tuple[dict, bool]:
    if "anyOf" in prop:
        variants = [v for v in prop["anyOf"] if v.get("type") != "null"]
        merged = {**variants[0], **{k: v for k, v in prop.items() if k != "anyOf"}}
        return merged, len(variants) < len(prop["anyOf"])
    return prop, False


def _enum_values(schema: dict, defs: dict) -> list | None:
    return _resolve(schema, defs).get("enum")


def _type_of(schema: dict, defs: dict) -> str | None:
    return _resolve(schema, defs).get("type")


@pytest.mark.parametrize("name", sorted(COMPONENTS))
def test_every_component_has_a_model(name):
    assert name in COMPONENT_MODELS or COMPONENTS[name].get("type") == "string", f"No model for {name}"


@pytest.mark.parametrize("name", sorted(COMPONENT_MODELS))
def test_model_matches_component(name):
    spec = COMPONENTS[name]
    model = COMPONENT_MODELS[name]
    is_request = spec.get("additionalProperties") is False
    generated = model.model_json_schema(mode="validation" if is_request else "serialization", by_alias=True)
    defs = {**generated.get("$defs", {}), name: generated}
    spec_defs = COMPONENTS

    assert set(generated["properties"]) == set(spec["properties"]), "property names differ"
    assert set(generated.get("required", [])) == set(spec.get("required", [])), "required fields differ"
    if is_request:
        assert generated.get("additionalProperties") is False, "request model must forbid extra fields"

    for prop_name, spec_prop_raw in spec["properties"].items():
        spec_prop, spec_nullable = _spec_prop(spec_prop_raw)
        model_prop_raw = generated["properties"][prop_name]
        model_prop, model_nullable = _model_prop(model_prop_raw)
        where = f"{name}.{prop_name}"

        # A required spec field is never null; optional, non-nullable fields are omitted rather than null.
        if spec_nullable:
            assert model_nullable, f"{where} must be nullable"
        elif prop_name in spec.get("required", []):
            assert not model_nullable, f"{where} must not be nullable"

        spec_target = _resolve(spec_prop, spec_defs)
        model_target = _resolve(model_prop, defs)
        if _ref_name(spec_prop) and spec_target.get("type") == "object":
            assert _ref_name(model_prop) == _ref_name(spec_prop), f"{where} must reference {_ref_name(spec_prop)}"
        assert _enum_values(spec_prop, spec_defs) == _enum_values(model_prop, defs), f"{where} enum differs"
        spec_type = _type_of(spec_prop, spec_defs)
        if spec_type:
            assert model_target.get("type") == spec_type, f"{where} type {model_target.get('type')} != {spec_type}"
        for key in CONSTRAINTS:
            assert spec_target.get(key) == model_target.get(key), f"{where} {key} differs"
        if spec_type == "array":
            spec_item, model_item = spec_target["items"], model_target["items"]
            if _ref_name(spec_item):
                assert _ref_name(spec_item) == _ref_name(model_item), f"{where} item type differs"
            else:
                inline = _resolve(model_item, defs)
                assert inline.get("type") == spec_item["type"], f"{where} item type differs"
                assert set(inline.get("properties", {})) == set(spec_item.get("properties", {}))
                assert set(inline.get("required", [])) == set(spec_item.get("required", []))
        if "default" in spec_prop:
            assert model_prop.get("default") == spec_prop["default"], f"{where} default differs"


def spec_operations():
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            if method in HTTP_METHODS:
                yield path, method, op


def test_every_spec_operation_is_served(app):
    prefix = app.state.settings.API_PREFIX
    served = {}

    def collect(routes, prefix_so_far=""):
        for route in routes:
            include = getattr(route, "include_context", None)
            if include is not None:  # FastAPI >= 0.140 nests included routers
                collect(include.included_router.routes, prefix_so_far + include.prefix)
                continue
            for method in getattr(route, "methods", set()) or set():
                served[(prefix_so_far + route.path, method.lower())] = route

    collect(app.routes)
    for path, method, op in spec_operations():
        route = served.get((prefix + path, method))
        assert route is not None, f"{method.upper()} {path} is not served"
        assert route.name == op["operationId"], f"{method.upper()} {path} handler should be {op['operationId']}"
        assert route.endpoint.__name__ == op["operationId"]


async def test_served_openapi_matches_spec(client):
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    served = response.json()
    assert set(served["paths"]) == set(SPEC["paths"])
    for path, item in SPEC["paths"].items():
        assert {m for m in served["paths"][path] if m in HTTP_METHODS} == {m for m in item if m in HTTP_METHODS}
    assert served["components"]["schemas"] == SPEC["components"]["schemas"]
