"""Responsive board references use semantic names and native bounded retries."""
import json
import pytest
from backend.teaching.board_blocks import has_screen_position_reference
from common_fixtures import synthetic_output_schema, synthetic_output_response
from test_teaching_assessments import env, database, lesson, LearningAdapter, BOARD  # noqa: F401


@pytest.mark.parametrize("value",[
    "看右侧代码，找到入口。", "左边的这段代码就是入口。", "上方表格比较了职责。",
    "下方的示意图描述了请求。", "Look at the code on the right.",
])
def test_screen_position_is_not_a_stable_board_reference(value):
    assert has_screen_position_reference(value)


@pytest.mark.parametrize("value",[
    "请看HTML骨架示例这段代码。", "左子树比根节点小，右子树比根节点大。",
    "等号右侧表达式的值会赋给左侧变量。", "第3行代码声明了入口。",
])
def test_content_positions_and_named_blocks_remain_valid(value):
    assert not has_screen_position_reference(value)


def test_native_agent_retries_unverified_screen_position_and_keeps_named_reference(env):
    _, _, start = lesson(env)
    client, state, owner, headers = env

    class Adapter(LearningAdapter):
        step_attempts = 0
        reference_policy = None

        def generate(self, messages, **options):
            if "steps" not in synthetic_output_schema(options).get("properties", {}):
                return super().generate(messages, **options)
            context = next(json.loads(message["content"]) for message in messages
                if message["role"] == "user" and '"current_stage"' in message.get("content", ""))
            self.reference_policy = context["board_reference_policy"]
            self.step_attempts += 1
            prefix = "看右侧代码，" if self.step_attempts == 1 else "看键值查找示例，"
            value = prefix+"我们先明确输入和输出，再按唯一标签寻找对应值，检查边界后用新的例子验证结论。"*3
            return synthetic_output_response({"steps":[{"alignment_version":1,"title":"合成命名引用","text":value,
                "board":{"title":"Synthetic board"},
                "beats":[{"id":"main","text":value,"blocks":[{"id":"lookup","type":"text","text":"唯一标签"}],
                    "cues":[{"block_id":"lookup","target":"text","quote":"唯一标签"}]}],
                "phase":context["current_stage"]["phase"],
                "covered_goal_ids":context["current_stage"]["goal_ids"],
                "covered_scope_capability_ids":context["current_stage"].get("scope_capability_ids", []),
                "lesson_finished":False}]}, options)

    adapter = Adapter()
    state.teaching.model_adapter_factory = lambda actor, task: adapter
    result = state.teaching.run_step_generation(start["task_id"])
    assert result["status"] == "succeeded"
    assert adapter.step_attempts == 2
    assert adapter.reference_policy["reference_by"] == "block_title_or_semantic_name"
    view = client.get(f"/api/teaching/{start['conversation_id']}", headers=headers).json()
    assert view["steps"][0]["text"].startswith("看键值查找示例，")


def test_native_agent_repairs_ambiguous_graph_branches_within_existing_budget(env):
    _, _, start = lesson(env)
    client, state, owner, headers = env

    class Adapter(LearningAdapter):
        step_attempts = 0
        saw_graph_policy = False

        def generate(self, messages, **options):
            if "steps" not in synthetic_output_schema(options).get("properties", {}):
                return super().generate(messages, **options)
            self.step_attempts += 1
            self.saw_graph_policy |= any("图表达约束" in m.get("content", "") for m in messages if m["role"] == "system")
            context = next(json.loads(m["content"]) for m in messages
                if m["role"] == "user" and '"current_stage"' in m.get("content", ""))
            labels = ("", "") if self.step_attempts == 1 else ("存在", "不存在")
            graph = {"id":"decision","type":"graph","diagram":"flowchart",
                "nodes":[{"id":"a","label":"键是否存在？","shape":"decision"},
                    {"id":"b","label":"读取对应值"},{"id":"c","label":"处理缺失键"}],
                "edges":[{"from_id":"a","to_id":"b","label":labels[0]},
                    {"from_id":"a","to_id":"c","label":labels[1]}]}
            first="节点包括键是否存在？读取对应值；处理缺失键。"
            edge_quotes=["键是否存在？存在时读取对应值。","键是否存在？不存在时处理缺失键。"]
            text=first+"".join(edge_quotes)+"再用一个具体例子验证不同路径，并确认条件和对应操作保持一致。"*3
            cues=[{"block_id":"decision","target":"node:"+node["id"],"quote":node["label"]} for node in graph["nodes"]]
            cues.extend({"block_id":"decision","target":f"edge:{index}","quote":quote} for index,quote in enumerate(edge_quotes))
            return synthetic_output_response({"steps":[{"alignment_version":1,"title":"合成分支说明",
                "text":text,"beats":[{"id":"main","text":text,"blocks":[graph],"cues":cues}],
                "board":{"title":"合成关系图","format_version":3},
                "phase":context["current_stage"]["phase"],
                "covered_goal_ids":context["current_stage"]["goal_ids"],
                "covered_scope_capability_ids":context["current_stage"].get("scope_capability_ids", []),
                "lesson_finished":False}]}, options)

    adapter = Adapter()
    state.teaching.model_adapter_factory = lambda actor, task: adapter
    result = state.teaching.run_step_generation(start["task_id"])
    assert result["status"] == "succeeded", result
    assert adapter.step_attempts == 2 and adapter.saw_graph_policy
    view = client.get(f"/api/teaching/{start['conversation_id']}", headers=headers).json()
    edges = view["steps"][0]["board"]["presentation"]["blocks"][0]["edges"]
    assert [edge["label"] for edge in edges] == ["存在", "不存在"]
