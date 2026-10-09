"""Generation-only strict subclasses; persisted model parsing is unchanged."""
from __future__ import annotations

from threading import RLock
import types
from typing import Annotated, Any, ForwardRef, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, create_model

_CACHE: dict[type[BaseModel], type[BaseModel]] = {}
_LOCK = RLock()


def _models(annotation: Any):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    elif get_origin(annotation) is Annotated:
        yield from _models(get_args(annotation)[0])
    elif get_origin(annotation) is not Literal:
        for argument in get_args(annotation):
            yield from _models(argument)


def _rewrite(annotation: Any, references: dict[type[BaseModel], ForwardRef]) -> Any:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _CACHE.get(annotation) or references[annotation]
    origin, arguments = get_origin(annotation), get_args(annotation)
    if not arguments or origin is Literal:
        return annotation
    if origin is Annotated:
        return Annotated[_rewrite(arguments[0], references), *arguments[1:]]
    rewritten = tuple(_rewrite(argument, references) for argument in arguments)
    if origin in (Union, types.UnionType):
        return Union[rewritten]
    copier = getattr(annotation, "copy_with", None)
    if copier is not None:
        return copier(rewritten)
    return origin[rewritten[0] if len(rewritten) == 1 else rewritten]


def generation_model(model: type[BaseModel]) -> type[BaseModel]:
    """Cache a recursive extra-forbid subclass without modifying domain classes."""
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError("generation_model requires a Pydantic BaseModel")
    with _LOCK:
        if model in _CACHE:
            return _CACHE[model]
        pending: dict[type[BaseModel], ForwardRef] = {}
        def discover(original):
            if original in _CACHE or original in pending:
                return
            pending[original] = ForwardRef(f"GenerationModel{len(pending)}")
            for field in original.model_fields.values():
                for nested in _models(field.annotation):
                    discover(nested)
        discover(model)
        variants = {}
        # Build the whole graph before resolving references, including cycles.
        for original in pending:
            fields = {}
            for name, field in original.model_fields.items():
                details = field.asdict()
                annotation = _rewrite(details["annotation"], pending)
                if details["metadata"]:
                    annotation = Annotated[annotation, *details["metadata"]]
                fields[name] = (annotation, Field(**details["attributes"]))
            config = ConfigDict(**original.model_config)
            if not original.__pydantic_root_model__:
                config["extra"] = "forbid"
            variants[original] = create_model(f"{original.__name__}Generation",
                __base__=original, __config__=config, __module__=__name__, **fields)
        namespace = {reference.__forward_arg__: variants[original]
                     for original, reference in pending.items()}
        for variant in variants.values():
            variant.model_rebuild(force=True, _types_namespace=namespace)
        _CACHE.update(variants)
        return _CACHE[model]


__all__ = ["generation_model"]
