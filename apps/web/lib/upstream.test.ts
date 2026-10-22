// node --test (Node 22 strips the types): the proxy's upstream classification.
import assert from "node:assert/strict";
import { test } from "node:test";

import { UNAVAILABLE_CODE, UNAVAILABLE_STATUS, classifyUpstream } from "./upstream.ts";

test("a JSON answer passes through with its own status and body, byte for byte", () => {
  for (const status of [200, 202, 401, 403, 404, 409, 422, 429, 503]) {
    const text = JSON.stringify({ detail: "x", status });
    const r = classifyUpstream(status, "application/json; charset=utf-8", text);
    assert.equal(r.status, status);
    assert.equal(r.body, text);
    assert.equal(r.replaced, false);
  }
});

test("an HTML error page from a tunnel or CDN becomes the unavailable error", () => {
  const html = "<!DOCTYPE html><html><body>502 Bad Gateway — origin https://internal.example:8000 down</body></html>";
  for (const status of [502, 530, 503, 504, 200, 403]) {
    const r = classifyUpstream(status, "text/html; charset=UTF-8", html);
    assert.equal(r.status, UNAVAILABLE_STATUS);
    assert.equal(r.replaced, true);
    const body = JSON.parse(r.body);
    assert.equal(body.code, UNAVAILABLE_CODE);
    assert.doesNotMatch(r.body, /html|internal\.example|8000/i);
  }
});

test("non-JSON media the API serves on a success passes through (report charts)", () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>';
  const r = classifyUpstream(200, "image/svg+xml", svg);
  assert.equal(r.replaced, false);
  assert.equal(r.body, svg);
  assert.equal(r.contentType, "image/svg+xml");
  // but not on a server error, and never HTML
  assert.equal(classifyUpstream(502, "image/svg+xml", svg).replaced, true);
  assert.equal(classifyUpstream(200, "text/html", "<html>portal</html>").replaced, true);
});

test("a truncated or non-JSON body declared as JSON is not forwarded", () => {
  const r = classifyUpstream(200, "application/json", '{"players": [');
  assert.equal(r.status, UNAVAILABLE_STATUS);
  assert.equal(r.replaced, true);
});

test("an empty body is only acceptable for a success", () => {
  assert.equal(classifyUpstream(204, null, "").replaced, false);
  assert.equal(classifyUpstream(502, null, "").status, UNAVAILABLE_STATUS);
  assert.equal(classifyUpstream(500, "text/plain", "").replaced, true);
});

test("the replacement never carries secrets or internals", () => {
  const r = classifyUpstream(500, "text/plain", "Traceback (most recent call last): x-api-key: s3cr3t");
  assert.doesNotMatch(r.body, /Traceback|s3cr3t|x-api-key/);
});
