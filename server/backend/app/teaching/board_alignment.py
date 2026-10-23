"""Versioned private references from visible board objects to the exact narration."""
import re
import math
import unicodedata

PROMPT = "board_alignment_v1"
GENERATION_POLICY = (
    "板书务必与当前短段的讲稿规模一致，优先使用1至3个短语块；每个beat最多4个blocks，全文最多6个beats。"
    "文字块text只取本段已经逐字说出的一个短语，"
    "不要写整段摘要后仅引用其中几个词。块title可以省略；填写时也必须在讲稿中逐字出现并单独引用。"
    "不要为了版式添加未说过的表头、图名、节点或边标签；只有本段能解释全部可见目标时才使用复杂图表。"
    "最小合法例：beat.text='先校验输入，再调用工具。'；"
    "blocks=[{id:'check',type:'text',text:'校验输入'},{id:'call',type:'text',text:'调用工具'}]；"
    "cues=[{block_id:'check',target:'text',quote:'校验输入'},"
    "{block_id:'call',target:'text',quote:'调用工具'}]。"
    "每个cue必须对应正确的块和目标字段；完整文字块需要完整短语的引用。"
    "结构校验指出具体目标后，只按讲稿原句修正该目标，未讲到的板书不要生成。"
)


def exact_subtitles(text, cues):
    """Accept exact text coverage and ordered time spans, including punctuation gaps."""
    if not isinstance(cues, list) or not cues or not all(isinstance(cue, dict) for cue in cues):
        return False
    mapped = all(type(cue.get("display_start")) is int and type(cue.get("display_end")) is int for cue in cues)
    if not mapped and "".join(str(cue.get("text") or cue.get("word") or "") for cue in cues) != text:
        return False
    def punctuation(value):
        return all(char.isspace() or unicodedata.category(char).startswith("P") for char in value)
    cursor, time = 0, 0
    for cue in cues:
        value = cue.get("text") or cue.get("word") or ""
        begin, end_ms = cue.get("begin_ms"), cue.get("end_ms")
        if not isinstance(value, str) or not value or any(type(v) not in (int, float) or not math.isfinite(v) for v in (begin,end_ms)):
            return False
        start = cue["display_start"] if mapped else cursor
        end = cue["display_end"] if mapped else start + len(value)
        if begin < time or end_ms <= begin or start < cursor or end <= start or end > len(text):
            return False
        if text[start:end] != value or not punctuation(text[cursor:start]):
            return False
        cursor, time = end, end_ms
    return punctuation(text[cursor:])


def contract_snapshot(prompts, task_id):
    from ..prompts.service import PromptUnavailable
    try:
        return prompts.resolve_for_task(task_id, PROMPT)["template_snapshot"] + "\n" + GENERATION_POLICY
    except PromptUnavailable:
        return None


def require_alignment(candidate, existing_blocks=None):
    if candidate.alignment_version != 1:
        from pydantic_ai.exceptions import ModelRetry
        raise ModelRetry("本任务要求 alignment_version=1，每个板书目标须用 cues 绑定讲稿原句。")
    defined = {block.id for beat in candidate.beats for block in beat.blocks}
    for block in candidate.board.blocks if candidate.board else []:
        if block.id not in defined and block.model_dump() != (existing_blocks or {}).get(block.id):
            from pydantic_ai.exceptions import ModelRetry
            raise ModelRetry("新板书必须放在对应 beat.blocks 内；独立 board.blocks 只能引用已冻结的原块。")


def board_targets(block):
    targets = {}
    if block.title:
        targets["title"] = ([block.title], False)
    kind = block.type
    if kind == "text":
        targets["text"] = ([block.text], False)
    elif kind == "table":
        for row, cells in enumerate([block.columns, *block.rows]):
            for column, value in enumerate(cells):
                if value.strip():
                    targets[f"cell:{row}:{column}"] = ([value], False)
    elif kind == "graph":
        for node in block.nodes:
            targets[f"node:{node.id}"] = ([node.label, node.group], False)
        by_id = {node.id: node for node in block.nodes}
        for index, edge in enumerate(block.edges):
            values = [edge.label] if edge.label else [by_id[edge.from_id].label, by_id[edge.to_id].label]
            targets[f"edge:{index}"] = (values, False)
    elif kind == "code":
        for index, line in enumerate(block.code.splitlines(), 1):
            if line.strip():
                targets[f"line:{index}"] = ([line], True)
    elif kind == "formula":
        targets["formula"] = ([block.plain_text or block.formula], True)
    elif kind in ("array", "stack", "layers"):
        items = getattr(block, "cells", getattr(block, "frames", getattr(block, "layers", [])))
        for item in items:
            if kind == "array":
                values = [item.value, item.index, item.pointer]
            elif kind == "stack":
                values = [item.label, *item.variables, item.waiting_for]
            else:
                values = [item.label, item.detail]
            targets[f"item:{item.id}"] = (values, kind == "stack")
        if getattr(block, "note", ""):
            targets["note"] = ([block.note], False)
    elif kind == "figure":
        graphic = block.graphic
        if graphic.kind == "circuit":
            values = [*graphic.inputs, *graphic.outputs]
        elif graphic.kind == "surface":
            values = [graphic.expression]
        else:
            values = [c.expression for c in graphic.curves]
            values.extend(f"{p.x:g}, {p.y:g}" for p in graphic.points)
        targets["figure"] = (values, True)
    elif kind == "chart":
        targets["chart"] = ([*block.labels, *block.series_labels,
                              *(str(value) for row in block.values for value in row)], False)
    return targets


def _matches(values, quote, technical, prefix=""):
    compact = re.sub(r"\s+", "", quote)
    for value in filter(None, values):
        if not technical:
            # Preserve words, units, negation and names instead of accepting a paraphrase.
            if re.sub(r"\s+", "", value) not in compact:
                return False
            position = compact.find(re.sub(r"\s+", "", value))
            before = re.sub(r"\s+", "", prefix) + compact[:position]
            if re.search(r"(?:不|无|未|非|没有|不能|不得|无需|不必|不是|禁止|无法)$", before):
                return False
        else:
            # Code/formulas may be spoken naturally, but identifiers and numbers stay exact.
            source = re.sub(r"\\[A-Za-z]+|//[^\n]*", "", value)
            terms = re.findall(r"[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?", source)
            syntax = {"if", "else", "for", "while", "return", "const", "let", "var", "int",
                      "float", "double", "def", "class", "public", "void", "true", "false"}
            for term in terms:
                if term not in syntax and not re.search(rf"(?<![A-Za-z_0-9]){re.escape(term)}(?![A-Za-z_0-9])", quote):
                    return False
            operators = {"+": ("+", "加"), "-": ("-", "减"), "*": ("*", "乘", "解引用"),
                         "/": ("/", "除"), "=": ("=", "等于", "设为", "赋值"),
                         ">": (">", "大于"), "<": ("<", "小于"), "^": ("^", "次方"),
                         ">=": (">=", "大于等于"), "<=": ("<=", "小于等于"),
                         "==": ("==", "等于"), "!=": ("!=", "不等于"),
                         "++": ("++", "自增", "加一", "加1"), "--": ("--", "自减", "减一", "减1")}
            if any(not any(alias in quote for alias in operators[operator])
                   for operator in re.findall(r">=|<=|==|!=|\+\+|--|[+\-*/=<>^]", source)):
                return False
            if not terms and re.sub(r"\s+", "", value) not in compact:
                return False
    return True


def aligned_beats(beats, blocks):
    """Validate coverage and resolve code-point offsets; never log private text."""
    by_id = {block.id: block for block in blocks}
    targets = {ident: board_targets(block) for ident, block in by_id.items()}
    defined = {block.id for beat in beats for block in beat.blocks}
    covered, available, result = set(), set(by_id) - defined, []
    visible, closed, ranks = set(), set(), {}
    writing_block = None
    offset, previous = 0, -1
    for beat in beats:
        available.update(block.id for block in beat.blocks)
        cues = []
        located = []
        for index, cue in enumerate(beat.cues):
            start = -1
            for _ in range(cue.occurrence + 1):
                start = beat.text.find(cue.quote, start + 1)
                if start < 0:
                    raise ValueError("board cue quote missing")
            located.append((start, index, cue))
        # Quote positions are authoritative. Normalize list order without
        # changing narration, references, coverage or the board-writing rules.
        for start, _, cue in sorted(located, key=lambda item: (item[0], item[1])):
            key = (cue.block_id, cue.target)
            if cue.block_id not in available or cue.target not in targets.get(cue.block_id, {}):
                raise ValueError("board cue target unavailable")
            if cue.target.startswith("edge:"):
                edge = by_id[cue.block_id].edges[int(cue.target.split(":")[1])]
                if any((cue.block_id, f"node:{ident}") not in covered
                       for ident in (edge.from_id, edge.to_id)):
                    raise ValueError("board cue edge precedes endpoints")
            if offset + start < previous:
                raise ValueError("board cue order invalid")
            if cue.block_id in defined and key not in visible:
                if writing_block != cue.block_id:
                    if cue.block_id in closed:
                        raise ValueError("board cue returns to unfinished earlier block")
                    if writing_block is not None:
                        closed.add(writing_block)
                    writing_block = cue.block_id
                rank = list(targets[cue.block_id]).index(cue.target)
                if rank < ranks.get(cue.block_id, -1):
                    raise ValueError("board cue writes above previous target")
                ranks[cue.block_id] = rank
                visible.add(key)
                if by_id[cue.block_id].type == "graph":
                    visible.update((cue.block_id, target) for target in targets[cue.block_id])
            values, technical = targets[cue.block_id][cue.target]
            if not _matches(values, cue.quote, technical, beat.text[:start]):
                raise ValueError(f"board cue expression differs: {cue.block_id}/{cue.target}; "
                    "quote must cover this target's complete expression from the narration")
            previous = offset + start
            covered.add(key)
            cues.append({"block_id": cue.block_id, "target": cue.target,
                         "start": offset + start, "end": offset + start + len(cue.quote)})
        result.append({"id": beat.id, "unit_id": beat.id, "text": beat.text,
                       "block_ids": [block.id for block in beat.blocks],
                       "highlight_ids": beat.highlight_ids, "cues": cues})
        offset += len(beat.text)
    expected = {(ident, target) for ident, values in targets.items() if ident in defined for target in values}
    if expected - covered:
        missing = ", ".join(f"{ident}/{target}" for ident, target in sorted(expected-covered)[:24])
        raise ValueError("board cue coverage missing: " + missing +
            "; add exact narration references, or omit board content that was not narrated")
    return result


def scene_beats(candidate):
    if candidate.alignment_version == 1:
        if candidate.board and any(getattr(candidate.board, field, None)
                                   for field in ("points", "nodes", "edges", "code", "formula")):
            raise ValueError("aligned board requires typed blocks")
        result = aligned_beats(candidate.beats, candidate.board.blocks if candidate.board else [])
        for item, beat in zip(result, candidate.beats):
            if beat._resolved_source:
                item["pdf_focus"] = beat._resolved_source
        return result
    return [{"id": beat.id, "unit_id": beat.id, "text": beat.text,
             **({"pdf_focus": beat._resolved_source} if beat._resolved_source else {}),
             "block_ids": [block.id for block in beat.blocks], "highlight_ids": beat.highlight_ids}
            for beat in candidate.beats]
