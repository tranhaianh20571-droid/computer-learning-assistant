"""Typed, deterministic ink cover renderer; no generated code or external URLs."""
from typing import Literal
import io
import math
import random
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFilter


class InkRecipe(BaseModel):
    composition: Literal["grid", "landscape", "branches", "flow"] = "grid"
    density: int = Field(default=18, ge=6, le=36)
    ink_strength: float = Field(default=0.65, ge=0.25, le=0.9)
    accent: Literal["gray", "blue", "ochre"] = "gray"
    whitespace: float = Field(default=0.32, ge=0.2, le=0.6)


class CoverAnswer(BaseModel):
    recipe: InkRecipe
    description: str = Field(default="当代水墨封面", max_length=600)
    asset_id: str = Field(default="", max_length=80)


def render_ink(recipe: InkRecipe, seed: int, width=960, height=720) -> bytes:
    rng = random.Random(seed)
    paper = Image.new("RGB", (width, height), (245, 242, 233))
    # A small seeded paper tile avoids a Python loop over every output pixel.
    tile = Image.new("RGB", (96, 96))
    pixels = tile.load()
    for y in range(96):
        for x in range(96):
            n = rng.choice((-2, -1, 0, 0, 0, 1, 2))
            pixels[x, y] = (245 + n, 242 + n, 233 + n)
    for y in range(0, height, 96):
        for x in range(0, width, 96):
            paper.paste(tile, (x, y))
    color = {"gray": (39, 43, 42), "blue": (55, 78, 81), "ochre": (102, 82, 57)}[recipe.accent]
    wash = Image.new("RGBA", paper.size)
    wd = ImageDraw.Draw(wash)
    left, right = width * .14, width * (1 - recipe.whitespace / 2)
    top, bottom = height * .16, height * .85
    # Broad translucent strokes and blurred edges mimic diluted ink on paper.
    for _ in range(5):
        cx, cy = rng.uniform(left, right), rng.uniform(top, bottom)
        rx, ry = rng.uniform(50, 150), rng.uniform(20, 75)
        wd.ellipse((cx-rx, cy-ry, cx+rx, cy+ry), fill=(*color, rng.randrange(8, 22)))
    paper = Image.alpha_composite(paper.convert("RGBA"), wash.filter(ImageFilter.GaussianBlur(24)))
    marks = Image.new("RGBA", paper.size)
    draw = ImageDraw.Draw(marks)
    def stroke(points, strength=1, thickness=2):
        alpha = min(230, int(230 * recipe.ink_strength * strength))
        for i in range(len(points)-1):
            taper = .6 + .6 * math.sin(math.pi * (i+1)/len(points))
            draw.line((points[i], points[i+1]), fill=(*color, int(alpha*rng.uniform(.7, 1))),
                      width=max(1, int(thickness*taper)))
    if recipe.composition == "grid":
        spacing = (right-left)/recipe.density
        for i in range(recipe.density+1):
            x = left+i*spacing
            points = [(x+math.sin(j*.34+i)*1.8+rng.uniform(-.8,.8), top+j*(bottom-top)/80) for j in range(81)]
            stroke(points, rng.uniform(.65, 1.2), 3 if i % 4 else 5)
        for i in range(int((bottom-top)/spacing)+1):
            y = top+i*spacing
            stroke([(left+j*(right-left)/90, y+math.sin(j*.26+i)*1.6+rng.uniform(-.7,.7)) for j in range(91)], .85, 3 if i % 3 else 4)
        for _ in range(5):
            x, y = rng.uniform(left, right-50), rng.uniform(top, bottom-25)
            for j in range(rng.randrange(8, 18)):
                stroke([(x,y+j*2),(x+rng.uniform(12,55),y+j*2+rng.uniform(-2,2))], 1.2, 4)
    elif recipe.composition == "landscape":
        for layer in range(5):
            baseline = height*(.44+layer*.075)
            points = [(left+j*(right-left)/100,
                       baseline-abs(math.sin(j*.065+layer*.8))*rng.uniform(65,100)) for j in range(101)]
            mist = Image.new("RGBA", paper.size); md = ImageDraw.Draw(mist)
            md.polygon(points+[(right,bottom),(left,bottom)], fill=(*color, 12+layer*6))
            paper = Image.alpha_composite(paper, mist.filter(ImageFilter.GaussianBlur(3)))
            stroke(points, .25+layer*.13, 2)
    elif recipe.composition == "branches":
        def branch(x,y,length,angle,depth):
            end=(x+math.cos(angle)*length,y+math.sin(angle)*length)
            pts=[(x+(end[0]-x)*t/30+math.sin(t*.5)*.9,y+(end[1]-y)*t/30) for t in range(31)]
            stroke(pts, .6+depth*.08, max(1,depth))
            if depth:
                branch(*end,length*.68,angle-rng.uniform(.25,.55),depth-1)
                branch(*end,length*.64,angle+rng.uniform(.25,.55),depth-1)
        branch(width*.43,bottom,height*.21,-math.pi/2,5)
    else:
        for i in range(recipe.density):
            phase=rng.uniform(-.3,.3)
            pts=[(left+j*(right-left)/120, height*.5+math.sin(j*.04+phase)*height*.19+i*3) for j in range(121)]
            stroke(pts,rng.uniform(.3,.9),2)
        for i in range(7):
            x=width*.65+i*12
            stroke([(x,top+j*(bottom-top)/70) for j in range(71)], .4)
    paper = Image.alpha_composite(paper, marks)
    out=io.BytesIO(); paper.convert("RGB").save(out,format="PNG",optimize=True)
    return out.getvalue()
