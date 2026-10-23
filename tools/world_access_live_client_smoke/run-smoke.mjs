import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "../world_access_client_smoke/node_modules/typescript/lib/typescript.js";

const MAX_SOURCE_BYTES = 256 * 1024;
const MAX_FIXTURE_BYTES = 1024 * 1024;
const MAX_CALLS = 9;
const LIVE_KIND = "live_generated_world_access_development";
const CONTRACT_REVISION = "0.1.0-rc.4";

function parseArguments(argv) {
  const expected = new Set([
    "--client",
    "--success-fixture",
    "--selection-fixture",
    "--problem-fixture",
  ]);
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!expected.has(name) || value === undefined || values.has(name)) {
      throw new Error("expected unique client, success, selection, and problem fixture arguments");
    }
    values.set(name, resolve(value));
  }
  if (values.size !== expected.size) {
    throw new Error("expected unique client, success, selection, and problem fixture arguments");
  }
  return {
    client: values.get("--client"),
    successFixture: values.get("--success-fixture"),
    selectionFixture: values.get("--selection-fixture"),
    problemFixture: values.get("--problem-fixture"),
  };
}

async function boundedJson(path) {
  const bytes = await readFile(path);
  if (bytes.length === 0 || bytes.length > MAX_FIXTURE_BYTES) {
    throw new Error("client smoke input is outside its byte bound");
  }
  return JSON.parse(bytes.toString("utf8"));
}

async function compileClient(clientPath, temporaryRoot) {
  const source = await readFile(clientPath);
  if (source.length === 0 || source.length > MAX_SOURCE_BYTES) {
    throw new Error("generated client is outside its byte bound");
  }
  const outputRoot = join(temporaryRoot, "compiled");
  const configPath = join(temporaryRoot, "tsconfig.json");
  await writeFile(
    configPath,
    JSON.stringify({
      compilerOptions: {
        target: "ES2022",
        module: "ES2022",
        moduleResolution: "Bundler",
        lib: ["ES2022", "DOM"],
        strict: true,
        noEmitOnError: true,
        isolatedModules: true,
        verbatimModuleSyntax: true,
        rootDir: dirname(clientPath),
        outDir: outputRoot,
      },
      files: [clientPath],
    }),
    { encoding: "utf8", mode: 0o600 },
  );
  const parsed = ts.getParsedCommandLineOfConfigFile(configPath, {}, {
    ...ts.sys,
    onUnRecoverableConfigFileDiagnostic(diagnostic) {
      throw new Error(ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"));
    },
  });
  if (parsed === undefined) {
    throw new Error("TypeScript configuration could not be parsed");
  }
  const program = ts.createProgram(parsed.fileNames, parsed.options);
  const emitted = program.emit();
  const diagnostics = ts
    .getPreEmitDiagnostics(program)
    .concat(emitted.diagnostics)
    .filter((diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error);
  if (diagnostics.length > 0 || emitted.emitSkipped) {
    const formatHost = {
      getCanonicalFileName: (path) => path,
      getCurrentDirectory: () => process.cwd(),
      getNewLine: () => "\n",
    };
    throw new Error(
      `TypeScript compilation failed: ${ts.formatDiagnostics(diagnostics, formatHost).trim()}`,
    );
  }
  await writeFile(join(outputRoot, "package.json"), '{"type":"module"}\n', {
    encoding: "utf8",
    mode: 0o600,
  });
  return join(outputRoot, "world_access_client.js");
}

function header(headers, name) {
  return Object.entries(headers ?? {}).find(
    ([key]) => key.toLowerCase() === name.toLowerCase(),
  )?.[1];
}

function assertLiveIdentity(value) {
  assert.equal(value.contract.revision, CONTRACT_REVISION);
  assert.equal(value.preview.kind, LIVE_KIND);
  assert.equal(value.preview.provider_calls, true);
  assert.equal(value.preview.live_forge_evidence, true);
  assert.equal(value.preview.live_attendee_execution, true);
  assert.equal(value.preview.live_capability_evidence, false);
  assert.equal(value.preview.loopback_only, true);
  assert.equal(value.preview.process_local, true);
  assert.deepEqual(value.preview.capabilities_enabled, []);
}

async function exerciseClient(Client, success, selectionResponse, problem) {
  const calls = [];
  const priorFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    if (calls.length >= MAX_CALLS) {
      throw new Error("bounded fake transport call limit exceeded");
    }
    calls.push({ url: String(url), init });
    if (calls.length === 6) {
      return new Response(JSON.stringify(selectionResponse), {
        status: 200,
        headers: { "Content-Type": "application/json; charset=utf-8" },
      });
    }
    if (calls.length === 7) {
      return new Response(JSON.stringify(problem), {
        status: 409,
        headers: { "Content-Type": "application/problem+json; charset=utf-8" },
      });
    }
    if (calls.length === 8) {
      return new Response(JSON.stringify(success), {
        status: 200,
        headers: { "Content-Type": "application/problem+json" },
      });
    }
    if (calls.length === 9) {
      return new Response(null, { status: 200 });
    }
    return new Response(JSON.stringify(success), {
      status: 200,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  };

  try {
    const client = new Client("http://127.0.0.1:43123", {
      bearer: "smoke-only-fake-bearer",
    });
    const identity = await client.identity();
    assert.deepEqual(identity, success);
    assertLiveIdentity(identity);
    client.setExpectedEpoch(success.service_epoch);
    const actor = "actor:smoke_context_00000001";
    const promotionId = "generated-access-promotion:smoke/value?x";
    const promotionBody = { forge_job_reference: "forge-reference:smoke" };
    const revealBody = {
      expected_access_record_id: "generated-access-record:smoke_00000001",
    };
    const exchangeBody = {
      offering_id: "offering:smoke",
      offering_revision: "offering-revision:smoke-aaaaaaaaaaaaaaaa",
      notice_version_presented: "notice:smoke",
    };
    await client.promote(actor, promotionBody, "smoke-promote");
    await client.promotion(actor, promotionId);
    await client.reveal(actor, promotionId, revealBody, "smoke-reveal");
    await client.catalog(actor);
    const selection = await client.exchange(actor, exchangeBody, "smoke-exchange");
    assert.deepEqual(selection, selectionResponse);
    assertLiveIdentity(selection);
    assert.match(selection.launch_binding_sha256, /^[a-f0-9]{64}$/);
    assert.equal(typeof selection.launch_grant, "string");
    assert.equal(typeof selection.selection.offering_id, "string");
    assert.equal(typeof selection.selection.offering_revision, "string");
    assert.equal(typeof selection.selection.origin.kind, "string");
    assert.equal(typeof selection.selection.display.title, "string");
    assert.equal(typeof selection.selection.notice_version, "string");
    assert.equal(typeof selection.replayed, "boolean");
    await assert.rejects(
      client.exchange(actor, exchangeBody, "smoke-exchange-rejection"),
      (error) => error.status === 409 && assert.deepEqual(error.value, problem) === undefined,
    );
    await assert.rejects(client.identity(), /Content-Type/);
    await assert.rejects(client.identity(), /Content-Type/);

    const encodedPromotion = "generated-access-promotion%3Asmoke%2Fvalue%3Fx";
    const expected = [
      ["GET", "/world-access/v1/identity", undefined, undefined],
      ["POST", "/world-access/v1/promotions", "smoke-promote", promotionBody],
      ["GET", `/world-access/v1/promotions/${encodedPromotion}`, undefined, undefined],
      ["POST", `/world-access/v1/promotions/${encodedPromotion}/reveal`, "smoke-reveal", revealBody],
      ["GET", "/world-access/v1/catalog", undefined, undefined],
      ["POST", "/world-access/v1/launch-selections", "smoke-exchange", exchangeBody],
      ["POST", "/world-access/v1/launch-selections", "smoke-exchange-rejection", exchangeBody],
      ["GET", "/world-access/v1/identity", undefined, undefined],
      ["GET", "/world-access/v1/identity", undefined, undefined],
    ];
    assert.equal(calls.length, MAX_CALLS);
    for (let index = 0; index < calls.length; index += 1) {
      const call = calls[index];
      const [method, path, idempotencyKey, body] = expected[index];
      assert.equal(call.url, `http://127.0.0.1:43123${path}`);
      assert.equal(call.init.method, method);
      assert.equal(call.init.redirect, "error");
      assert.equal(call.init.cache, "no-store");
      assert.equal(header(call.init.headers, "Authorization"), "Bearer smoke-only-fake-bearer");
      assert.equal(
        header(call.init.headers, "Accept"),
        "application/json, application/problem+json",
      );
      if (index === 0 || index === 7 || index === 8) {
        assert.equal(header(call.init.headers, "X-Dreamland-Expected-Epoch"), undefined);
        assert.equal(header(call.init.headers, "X-Dreamland-Actor-Context"), undefined);
      } else {
        assert.equal(header(call.init.headers, "X-Dreamland-Expected-Epoch"), success.service_epoch);
        assert.equal(header(call.init.headers, "X-Dreamland-Actor-Context"), actor);
      }
      assert.equal(header(call.init.headers, "Idempotency-Key"), idempotencyKey);
      assert.equal(header(call.init.headers, "Content-Type"), body === undefined ? undefined : "application/json");
      assert.equal(call.init.body, body === undefined ? undefined : JSON.stringify(body));
    }
  } finally {
    globalThis.fetch = priorFetch;
  }
}

async function main() {
  const nodeMajor = Number.parseInt(process.versions.node.split(".", 1)[0], 10);
  if (!Number.isInteger(nodeMajor) || nodeMajor < 20) {
    throw new Error("live World Access client smoke requires Node.js 20 or newer");
  }
  const arguments_ = parseArguments(process.argv.slice(2));
  const success = await boundedJson(arguments_.successFixture);
  const selection = await boundedJson(arguments_.selectionFixture);
  const problem = await boundedJson(arguments_.problemFixture);
  assertLiveIdentity(success);
  assertLiveIdentity(selection);
  assert.equal(selection.service_epoch, success.service_epoch);
  assert.match(selection.launch_binding_sha256, /^[a-f0-9]{64}$/);
  assert.equal(typeof problem.error?.code, "string");
  const temporaryRoot = await mkdtemp(join(tmpdir(), "dreamland-world-access-live-client-"));
  try {
    const compiledPath = await compileClient(arguments_.client, temporaryRoot);
    const module = await import(pathToFileURL(compiledPath).href);
    assert.equal(module.WORLD_ACCESS_CONTRACT_REVISION, CONTRACT_REVISION);
    assert.equal(typeof module.WorldAccessPrivateClient, "function");
    await exerciseClient(module.WorldAccessPrivateClient, success, selection, problem);
    process.stdout.write(
      JSON.stringify({
        compiled: true,
        content_type_mismatch_rejected: true,
        launch_binding_sha256_consumed: true,
        launch_selection_fields_consumed: true,
        live_identity_consumed: true,
        missing_content_type_rejected: true,
        node_major: nodeMajor,
        operations: 6,
        transport: "bounded_fake_fetch",
        typescript: ts.version,
      }) + "\n",
    );
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
