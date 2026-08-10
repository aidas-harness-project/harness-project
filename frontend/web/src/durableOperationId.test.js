import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";

import { clearDurableOperation, durableOperationId } from "./durableOperationId.js";

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

test("medical operation IDs survive remounts until server acknowledgement", async () => {
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: webcrypto });
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: memoryStorage() });

  const first = await durableOperationId("medical:CASE_0001:MRI_0001", "same-request", "medical");
  const retried = await durableOperationId("medical:CASE_0001:MRI_0001", "same-request", "medical");
  assert.equal(retried.operationId, first.operationId);

  clearDurableOperation(first.key);
  const next = await durableOperationId("medical:CASE_0001:MRI_0001", "same-request", "medical");
  assert.notEqual(next.operationId, first.operationId);
});

test("medical operation IDs fail closed without durable browser storage", async () => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: null });
  await assert.rejects(
    durableOperationId("scope", "request", "medical"),
    /durable browser operation storage is unavailable/,
  );
});

test("medical operation acknowledgement fails closed when its durable ID remains", () => {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: () => "medical:still-present",
      removeItem: () => {},
    },
  });
  assert.throws(
    () => clearDurableOperation("pending-key"),
    /operation ID did not clear/,
  );
});
