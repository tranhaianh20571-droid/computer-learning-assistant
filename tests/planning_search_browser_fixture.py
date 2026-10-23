"""SDK FunctionModel and synthetic search wire for the isolated browser runner."""
import json

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel


def configure(state, record):
    from native_model_support import _request
    from test_v2_intake import InterviewAdapter
    from test_planning_quality import _plan, _route
    from app.planning.outputs import QualityReview
    from app.search.service import SearchService
    from test_anysearch_service import Adapter

    def response(messages, info):
        translated, options = _request(messages, info)
        schema = info.model_request_parameters.output_object
        props = schema.json_schema.get("properties", {}) if schema else {}
        record("intake" if "ready_to_plan" in props else "planning", info)
        if "coverage_areas" in props:
            output = _plan(areas=[
                {"area": f"Synthetic area {index}", "search_query": f"Synthetic targeted query {index}"}
                for index in range(9)
            ]).model_dump(mode="json")
        elif "verdict" in props:
            output = QualityReview().model_dump(mode="json")
        elif "stages" in props:
            output = _route(coverage_titles=[f"Synthetic area {index}" for index in range(9)])
        else:
            return ModelResponse(parts=[TextPart(InterviewAdapter(scope="route").generate(translated, **options)["content"])])
        return ModelResponse(parts=[TextPart(json.dumps(output))])

    state.planning.model_adapter_factory = lambda *_: FunctionModel(response,
        model_name="synthetic-search-planning-browser")
    state.planning.search_adapter_factory = lambda owner, task: SearchService(state.db,
        state.teaching.configs, owner_user_id=owner, task_id=task,
        adapter_factory=lambda *_: Adapter())
