import assert from "node:assert/strict";
import { test } from "node:test";
import { memoryProfileSections } from "../frontend/src/lib/memoryProfile.ts";

test("profile headings keep all four sections, paragraphs and additional sections", () => {
  const result = memoryProfileSections("## 用户身份\r\n身份正文\r\n\r\n## 背景基础\r\n基础一\r\n\r\n基础二\r\n## 用户偏好\r\n偏好\r\n## 能力与困难\r\n困难\r\n## 其他证据\r\n证据");
  assert.deepEqual(result.map(s => s.title), ["用户身份", "背景基础", "用户偏好", "能力与困难", "其他证据"]);
  assert.deepEqual(result[1].paragraphs, ["基础一", "基础二"]);
  assert.equal(result[4].paragraphs[0], "证据");
});
test("plain legacy profile and introductory content are preserved", () => {
  assert.deepEqual(memoryProfileSections("旧画像\n保留换行"), [{ title: "", paragraphs: ["旧画像\n保留换行"] }]);
  assert.deepEqual(memoryProfileSections("前言\n\n## 用户身份\n正文")[0], { title: "", paragraphs: ["前言"] });
  assert.deepEqual(memoryProfileSections(""), []);
});
test("fenced headings stay body text, and empty headings retain their section", () => {
  const result = memoryProfileSections("## 背景基础\n```python\n## example\n```\n## 用户偏好\n## 能力与困难");
  assert.deepEqual(result.map(s => s.title), ["背景基础", "用户偏好", "能力与困难"]);
  assert.equal(result[0].paragraphs[0], "```python\n## example\n```");
  assert.deepEqual(result[1].paragraphs, []);
});
test("heading delimiters are removed without interpreting HTML or body hashes", () => {
  const result = memoryProfileSections("### 用户身份 ###\n<img src=x onerror=alert(1)>\nC# 和 #标签");
  assert.equal(result[0].title, "用户身份");
  assert.equal(result[0].paragraphs[0], "<img src=x onerror=alert(1)>\nC# 和 #标签");
});
