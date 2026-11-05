// Browsers compile <input pattern> with the RegExp "v" flag. An invalid pattern is
// silently ignored (no validation) and logs a console error, so check them all here.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

for (const page of ["app.html", "index.html"]) {
  test(`${page}: every input pattern is a valid v-flag regular expression`, () => {
    const html = fs.readFileSync(path.join(__dirname, "..", "runtime", "web", page), "utf8");
    for (const [, pattern] of html.matchAll(/\spattern="([^"]*)"/g)) {
      assert.doesNotThrow(() => new RegExp(`^(?:${pattern})$`, "v"), `invalid pattern: ${pattern}`);
    }
  });
}

test("service label pattern accepts dotted and hyphenated labels", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "runtime", "web", "app.html"), "utf8");
  const pattern = html.match(/id="source-label"[^>]*pattern="([^"]*)"/)[1];
  const re = new RegExp(`^(?:${pattern})$`, "v");
  for (const ok of ["service", "service.name", "k8s-app", "_job"]) assert.ok(re.test(ok), ok);
  for (const bad of ["1service", "service name", ""]) assert.ok(!re.test(bad), bad);
});

// Parse the entire shipped script so handler-level checks cannot miss syntax errors.
test("console script compiles", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "runtime", "web", "app.html"), "utf8");
  assert.doesNotThrow(() => new (require("node:vm").Script)(html.match(/<script>([\s\S]*?)<\/script>/)[1]));
});
