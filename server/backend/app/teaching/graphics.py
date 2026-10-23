"""Bounded semantic figures and deterministic drawing checks; never execute code."""
import ast
import itertools
import math
import operator
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FUNCTIONS = {name: getattr(math, name) for name in ("sin", "cos", "tan", "sqrt", "exp", "log")}
FUNCTIONS["abs"] = abs
CONSTANTS = {"pi": math.pi, "e": math.e}
OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
             ast.Div: operator.truediv, ast.Pow: operator.pow}
IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_]{0,23}$"
GateKind = Literal["NOT", "AND", "OR", "XOR", "NAND", "NOR", "XNOR", "BUFFER"]


def expression_tree(source, variables=("x",)):
    if not isinstance(source, str) or not 0 < len(source) <= 240:
        raise ValueError("invalid expression length")
    try:
        tree = ast.parse(source.replace("^", "**"), mode="eval")
    except (SyntaxError, RecursionError):
        raise ValueError("invalid expression") from None
    if len(list(ast.walk(tree))) > 96:
        raise ValueError("expression too complex")

    def visit(node, depth=0):
        if depth > 16:
            raise ValueError("expression too deep")
        if isinstance(node, ast.Expression):
            visit(node.body, depth + 1)
        elif isinstance(node, ast.Constant):
            if type(node.value) not in (int, float) or not math.isfinite(node.value) or abs(node.value) > 1e6:
                raise ValueError("invalid number")
        elif isinstance(node, ast.Name):
            if node.id not in (*variables, *CONSTANTS):
                raise ValueError("unknown variable")
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            visit(node.operand, depth + 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            if isinstance(node.op, ast.Pow):
                power = node.right
                if isinstance(power, ast.UnaryOp) and isinstance(power.op, (ast.USub, ast.UAdd)):
                    power = power.operand
                if not isinstance(power, ast.Constant) or type(power.value) not in (int, float) or abs(power.value) > 12:
                    raise ValueError("power must be a small literal")
            visit(node.left, depth + 1)
            visit(node.right, depth + 1)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in FUNCTIONS and len(node.args) == 1 and not node.keywords):
            visit(node.args[0], depth + 1)
        else:
            raise ValueError("unsupported expression")
    visit(tree)
    return tree.body


def evaluate(tree, variables):
    def run(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return variables[node.id] if node.id in variables else CONSTANTS[node.id]
        if isinstance(node, ast.UnaryOp):
            return (-1 if isinstance(node.op, ast.USub) else 1) * run(node.operand)
        if isinstance(node, ast.BinOp):
            return OPERATORS[type(node.op)](run(node.left), run(node.right))
        return FUNCTIONS[node.func.id](run(node.args[0]))
    try:
        value = run(tree)
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) and abs(value) <= 1e12 else None
    except (ValueError, OverflowError, ZeroDivisionError, TypeError):
        return None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Bounds(Strict):
    x: tuple[float, float] = (-5, 5)
    y: tuple[float, float] = (-5, 5)

    @model_validator(mode="after")
    def ordered(self):
        for low, high in (self.x, self.y):
            if not -1e4 <= low < high <= 1e4 or high - low < 1e-4:
                raise ValueError("invalid coordinate bounds")
        return self


class Point(Strict):
    x: float = Field(ge=-1e4, le=1e4)
    y: float = Field(ge=-1e4, le=1e4)
    label: str = Field(default="", max_length=40)


class Curve(Strict):
    expression: str = Field(min_length=1, max_length=240)
    label: str = Field(default="", max_length=60)

    @field_validator("expression")
    @classmethod
    def safe(cls, value):
        expression_tree(value)
        return value


class PlotFigure(Strict):
    kind: Literal["plot"]
    bounds: Bounds = Field(default_factory=Bounds)
    curves: list[Curve] = Field(default_factory=list, max_length=4)
    points: list[Point] = Field(default_factory=list, max_length=12)

    def validate_visible(self):
        for p in self.points:
            if not self.bounds.x[0] <= p.x <= self.bounds.x[1] or not self.bounds.y[0] <= p.y <= self.bounds.y[1]:
                raise ValueError("reference point outside viewport")
        for curve in self.curves:
            tree = expression_tree(curve.expression)
            visible = 0
            for index in range(81):
                x = self.bounds.x[0] + (self.bounds.x[1] - self.bounds.x[0]) * (index + .37) / 81
                y = evaluate(tree, {"x": x})
                visible += y is not None and self.bounds.y[0] <= y <= self.bounds.y[1]
            if visible < 8:
                raise ValueError("reference curve has insufficient visible domain")


class SurfaceFigure(Strict):
    kind: Literal["surface"]
    expression: str = Field(min_length=1, max_length=240)
    bounds: Bounds = Field(default_factory=Bounds)

    @field_validator("expression")
    @classmethod
    def safe(cls, value):
        expression_tree(value, ("x", "y"))
        return value


class Gate(Strict):
    id: str = Field(pattern=IDENTIFIER)
    type: GateKind
    x: float = Field(default=240, ge=0, le=1200)
    y: float = Field(default=120, ge=0, le=800)


class Wire(Strict):
    source: str = Field(pattern=IDENTIFIER)
    target: str = Field(pattern=IDENTIFIER)
    port: int = Field(ge=0, le=1)


class CircuitFigure(Strict):
    kind: Literal["circuit"]
    inputs: list[str] = Field(min_length=1, max_length=6)
    outputs: list[str] = Field(min_length=1, max_length=3)
    gates: list[Gate] = Field(default_factory=list, max_length=32)
    wires: list[Wire] = Field(default_factory=list, max_length=96)

    @field_validator("inputs", "outputs")
    @classmethod
    def identifiers(cls, values):
        import re
        if len(set(values)) != len(values) or any(not re.fullmatch(IDENTIFIER, v) for v in values):
            raise ValueError("invalid terminal")
        return values

    @model_validator(mode="after")
    def references(self):
        ids = self.inputs + self.outputs + [g.id for g in self.gates]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate circuit identity")
        sources = set(self.inputs) | {g.id for g in self.gates}
        targets = set(self.outputs) | {g.id for g in self.gates}
        occupied = set()
        for wire in self.wires:
            if wire.source not in sources or wire.target not in targets or wire.source == wire.target:
                raise ValueError("invalid circuit reference")
            key = (wire.target, wire.port)
            if key in occupied:
                raise ValueError("multiple drivers")
            occupied.add(key)
            if wire.target in self.outputs and wire.port != 0:
                raise ValueError("invalid output port")
            gate = next((g for g in self.gates if g.id == wire.target), None)
            if gate and gate.type in ("NOT", "BUFFER") and wire.port != 0:
                raise ValueError("invalid gate port")
        return self


Graphic = Annotated[PlotFigure | SurfaceFigure | CircuitFigure, Field(discriminator="kind")]
QuestionGraphic = Annotated[PlotFigure | CircuitFigure, Field(discriminator="kind")]


def circuit_table(circuit):
    drivers = {(w.target, w.port): w.source for w in circuit.wires}
    gates = {g.id: g for g in circuit.gates}
    rows = []
    # Check every gate, including disconnected parts, before accepting a solution.
    for bits in itertools.product((False, True), repeat=len(circuit.inputs)):
        values = dict(zip(circuit.inputs, bits))
        visiting = set()

        def value(name):
            if name in values:
                return values[name]
            if name in visiting:
                raise ValueError("feedback loop")
            visiting.add(name)
            gate = gates.get(name)
            count = 1 if gate is None or gate.type in ("NOT", "BUFFER") else 2
            args = []
            for p in range(count):
                source = drivers.get((name, p))
                if source is None:
                    raise ValueError("unconnected input")
                args.append(value(source))
            if gate is None or gate.type == "BUFFER":
                result = args[0]
            elif gate.type == "NOT":
                result = not args[0]
            elif gate.type in ("AND", "NAND"):
                result = all(args)
            elif gate.type in ("OR", "NOR"):
                result = any(args)
            else:
                result = args[0] != args[1]
            if gate and gate.type in ("NAND", "NOR", "XNOR"):
                result = not result
            values[name] = result
            visiting.remove(name)
            return result
        for name in gates:
            value(name)
        rows.append(tuple(value(name) for name in circuit.outputs))
    return rows


class PlotDrawing(Strict):
    kind: Literal["plot"]
    bounds: Bounds = Field(default_factory=Bounds)
    starter: PlotFigure | None = None
    allow_expression: bool = True
    allow_sketch: bool = True
    allow_points: bool = True


class CircuitDrawing(Strict):
    kind: Literal["circuit"]
    inputs: list[str] = Field(min_length=1, max_length=6)
    outputs: list[str] = Field(min_length=1, max_length=3)
    allowed_gates: list[GateKind] = Field(default_factory=lambda: ["NOT", "AND", "OR"], min_length=1, max_length=8)
    starter: CircuitFigure | None = None

    @model_validator(mode="after")
    def terminals(self):
        CircuitFigure(kind="circuit", inputs=self.inputs, outputs=self.outputs)
        if self.starter:
            if self.starter.inputs != self.inputs or self.starter.outputs != self.outputs:
                raise ValueError("starter terminal mismatch")
            if any(g.type not in self.allowed_gates for g in self.starter.gates):
                raise ValueError("starter gate not allowed")
        return self


DrawingSpec = Annotated[PlotDrawing | CircuitDrawing, Field(discriminator="kind")]


class PlotAnswer(Strict):
    kind: Literal["plot"]
    curves: list[Curve] = Field(default_factory=list, max_length=4)
    points: list[Point] = Field(default_factory=list, max_length=12)
    strokes: list[list[Point]] = Field(default_factory=list, max_length=4)

    @field_validator("strokes")
    @classmethod
    def bounded(cls, strokes):
        if any(not 2 <= len(stroke) <= 300 for stroke in strokes):
            raise ValueError("invalid stroke length")
        return strokes


class CircuitAnswer(CircuitFigure):
    pass


DrawingAnswer = Annotated[PlotAnswer | CircuitAnswer, Field(discriminator="kind")]


def validate_drawing_answer(raw, spec):
    from pydantic import TypeAdapter
    answer = TypeAdapter(DrawingAnswer).validate_python(raw)
    if answer.kind != spec.kind:
        raise ValueError("drawing kind mismatch")
    if answer.kind == "circuit":
        if answer.inputs != spec.inputs or answer.outputs != spec.outputs:
            raise ValueError("terminal mismatch")
        if not answer.gates and not answer.wires:
            raise ValueError("empty drawing")
        if any(g.type not in spec.allowed_gates for g in answer.gates):
            raise ValueError("gate not allowed")
    else:
        if not (answer.curves or answer.points or answer.strokes):
            raise ValueError("empty drawing")
        if (answer.curves and not spec.allow_expression or answer.points and not spec.allow_points
                or answer.strokes and not spec.allow_sketch):
            raise ValueError("drawing tool not allowed")
    return answer


def drawing_grade(question, raw):
    """The private reference never leaves this deterministic grading boundary."""
    from pydantic import TypeAdapter
    spec = TypeAdapter(DrawingSpec).validate_python(question["drawing"])
    answer = validate_drawing_answer(raw, spec)
    solution = TypeAdapter(QuestionGraphic).validate_python(question["solution"])
    maximum = question["points"]
    if answer.kind == "circuit":
        try:
            expected, actual = circuit_table(solution), circuit_table(answer)
            ratio = sum(a == b for a, b in zip(expected, actual)) / len(expected)
            reason = "所有输入组合的输出均与目标逻辑一致。" if ratio == 1 else "部分输入组合的输出与目标逻辑不同。"
        except ValueError:
            ratio, reason = 0, "电路存在未连接的引脚或反馈环，请复核连接。"
    else:
        ratio = plot_similarity(solution, answer)
        reason = "图形在采样及标点容差内符合参考要求。" if ratio == 1 else "部分曲线或必需标点与参考要求不符。"
    earned = min(maximum if ratio == 1 else maximum - 1, max(0, round(maximum * ratio)))
    return {"question_id": question["id"], "earned": earned, "maximum": maximum,
            "reason": reason, "weaknesses": [] if earned == maximum else [question["goal"]],
            "drawing_answer": answer.model_dump(), "drawing_solution": solution.model_dump()}


def plot_similarity(solution, answer):
    bounds = solution.bounds
    dx, dy = bounds.x[1] - bounds.x[0], bounds.y[1] - bounds.y[0]
    tolerance = dy * .025
    reference = [expression_tree(c.expression) for c in solution.curves]
    proposed = [expression_tree(c.expression) for c in answer.curves]

    def stroke_value(stroke, x):
        for a, b in zip(stroke, stroke[1:]):
            if min(a.x, b.x) <= x <= max(a.x, b.x) and abs(a.x - b.x) > 1e-9:
                return a.y + (x - a.x) / (b.x - a.x) * (b.y - a.y)
        return None

    # One-to-one matching prevents duplicate submitted curves/points from earning
    # credit twice and penalizes extraneous visible geometry.
    scores = []
    available = list(range(len(proposed) + len(answer.strokes)))
    for tree in reference:
        samples = []
        for i in range(81):
            x = bounds.x[0] + dx * (i + .37) / 81
            y = evaluate(tree, {"x": x})
            if y is not None and bounds.y[0] <= y <= bounds.y[1]:
                samples.append((x, y))
        best, best_score = None, 0
        for index in available:
            hits = 0
            for x, y in samples:
                v = (evaluate(proposed[index], {"x": x}) if index < len(proposed)
                     else stroke_value(answer.strokes[index - len(proposed)], x))
                hits += v is not None and abs(v - y) <= tolerance
            score = hits / len(samples) if len(samples) >= 8 else 0
            if score > best_score:
                best, best_score = index, score
        if best is not None:
            available.remove(best)
        scores.append(best_score)
    scores.extend(0 for _ in available)
    points = list(answer.points)
    for expected in solution.points:
        found = next((p for p in points if abs(p.x - expected.x) <= dx * .025
                      and abs(p.y - expected.y) <= dy * .025), None)
        scores.append(1 if found else 0)
        if found:
            points.remove(found)
    scores.extend(0 for _ in points)
    return sum(scores) / len(scores) if scores else 0
