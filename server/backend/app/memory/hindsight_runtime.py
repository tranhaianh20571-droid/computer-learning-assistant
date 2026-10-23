"""Complete bank attribution for native foreground and background engine calls."""
import functools
import inspect


def install_bank_attribution(engine_type=None):
    from hindsight_api.engine.memory_engine import MemoryEngine, _current_bank_id
    engine_type = engine_type or MemoryEngine
    for name, method in inspect.getmembers(engine_type, inspect.iscoroutinefunction):
        if getattr(method, "_personal_bank_scope", False):
            continue
        signature = inspect.signature(method)
        if "bank_id" not in signature.parameters:
            continue
        def decorate(target, parameters):
            @functools.wraps(target)
            async def scoped(*args, **kwargs):
                bank = parameters.bind(*args, **kwargs).arguments.get("bank_id")
                if not isinstance(bank, str):
                    return await target(*args, **kwargs)
                token = _current_bank_id.set(bank)
                try:
                    return await target(*args, **kwargs)
                finally:
                    _current_bank_id.reset(token)
            scoped._personal_bank_scope = True
            return scoped
        setattr(engine_type, name, decorate(method, signature))
