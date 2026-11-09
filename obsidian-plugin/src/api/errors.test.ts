import { expect, it } from "vitest";
import { ApiError, apiErrorOf, detailOf, validationText } from "./errors";

it("apiErrorOf keeps the response's machine code (e.g. patches_off), null when there is none", () => {
  const e = apiErrorOf(409, JSON.stringify({ detail: "代码修改已关闭", code: "patches_off" }));
  expect([e.status, e.detail, e.code]).toEqual([409, "代码修改已关闭", "patches_off"]);
  expect(apiErrorOf(409, '{"detail":"x"}').code).toBeNull();
  expect(apiErrorOf(500, "boom").code).toBeNull();
  expect(new ApiError(409, "x").code).toBeNull();
});

it("extracts FastAPI detail strings, stringifies structured detail, falls back to text", () => {
  expect(detailOf(404, '{"detail":"unknown concept pca"}')).toBe("unknown concept pca");
  expect(detailOf(422, '{"detail":[{"loc":["body"]}]}')).toBe('[{"loc":["body"]}]');
  expect(detailOf(500, "boom")).toBe("boom");
  expect(detailOf(502, "")).toBe("HTTP 502");
});

it("ApiError carries status and detail", () => {
  const e = new ApiError(409, "session ended");
  expect([e.status, e.detail, e.message]).toEqual([409, "session ended", "409: session ended"]);
});

it("validationText joins the msg of a pydantic 422 body without the Value error prefix", () => {
  const body = JSON.stringify({ detail: [
    { type: "value_error", loc: ["body", "levels"], msg: "Value error, level of page_body cannot change", input: {}, ctx: {} },
    { type: "greater_than_equal", loc: ["body", "today_cap"], msg: "Input should be greater than or equal to 0", input: -1 }] });
  const detail = detailOf(422, body);
  expect(validationText(detail)).toBe("level of page_body cannot change; Input should be greater than or equal to 0");
  expect(validationText("plain text")).toBe("plain text");
  expect(validationText('[{"loc":["body"]}]')).toBe('[{"loc":["body"]}]');
});
