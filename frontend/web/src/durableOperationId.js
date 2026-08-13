const STORAGE_PREFIX = "aidas.pending-operation.v1";

function bytesToHex(bytes) {
  return [...bytes]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

async function signatureDigest(signature) {
  const payload = new TextEncoder().encode(signature);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", payload);
  return bytesToHex(new Uint8Array(digest));
}

export async function durableOperationId(scope, signature, prefix) {
  if (!globalThis.crypto?.subtle || !globalThis.localStorage) {
    throw new Error("durable browser operation storage is unavailable");
  }
  const digest = await signatureDigest(signature);
  const key = `${STORAGE_PREFIX}:${scope}:${digest}`;
  try {
    const existing = globalThis.localStorage.getItem(key);
    if (existing) return { key, operationId: existing };
    const operationId = `${prefix}:${globalThis.crypto.randomUUID()}`;
    globalThis.localStorage.setItem(key, operationId);
    if (globalThis.localStorage.getItem(key) !== operationId) {
      throw new Error("operation ID did not persist");
    }
    return { key, operationId };
  } catch (error) {
    throw new Error("durable browser operation storage is unavailable", {
      cause: error,
    });
  }
}

export function clearDurableOperation(key) {
  try {
    if (!globalThis.localStorage) throw new Error("storage is unavailable");
    globalThis.localStorage.removeItem(key);
    if (globalThis.localStorage.getItem(key) !== null) {
      throw new Error("operation ID did not clear");
    }
  } catch (error) {
    throw new Error("operation ID did not clear", { cause: error });
  }
}
