#!/usr/bin/env node
// DeltaHarness · TypeScript-AST environment reference scanner (used by VibeRescue; doctor embeds the shared block).
//
// Usage: node js_env_refs.cjs <projectRoot> < files.json      (files.json = ["rel/path.tsx", ...])
// Prints one JSON object:
//   {mode, typescriptVersion, files: {rel: {isClient, refs: [{key, line}], dynamic: [{line, reason}], parseDiagnostics}}}
//
// TypeScript is resolved from the scanned project (its own node_modules) or from DELTAHARNESS_TYPESCRIPT.
// Exit 3 when TypeScript cannot be resolved (callers then report the JS check as UNVERIFIED for client files).
"use strict";
const fs = require("fs");
const path = require("path");

// BEGIN-SHARED-ENV-ANALYZER (byte-identical copy lives in 04-vercel-clean-build-doctor.sh; a ratchet compares them)
// Allowlist (Round 9): only these forms are interpreted; every other contact with the environment, the process
// object, a global object, import.meta, dynamic import or a non-literal require is reported in `unverified`.
//   process.env.KEY | process.env["KEY"] | process.env["K" + "EY"] | const {KEY, ["K2"]: x} = process.env
//   import.meta.env.KEY | import.meta["env"].KEY
// where `process` is the runtime global (checker: undeclared/ambient) or a static import / require of
// "process" | "node:process" (`import {env} from "node:process"` and `const {env} = require("process")` give env).
// Static, non-computed properties other than `env` (process.version, window.location, import.meta.url) are allowed.
// Wrappers that do not change meaning are removed: (x), x as T, <T>x, x!, x satisfies T.
function createEnvAnalyzer(ts, entries) {
  const PROCESS_MODULES = new Set(["process", "node:process"]);
  const GLOBAL_OBJECTS = new Set(["globalThis", "global", "window", "self"]);
  // Global-object members that lead back to process/module loading (checked as a closed list).
  const GLOBAL_ESCAPE_MEMBERS = new Set(["globalThis", "global", "window", "self", "process", "require", "module", "eval", "Function", "setTimeout", "setInterval", "Reflect"]);
  // process methods that return the process object itself: allowed only as standalone statements.
  const PROCESS_CHAINING = new Set(["on", "once", "off", "removeListener", "addListener", "prependListener"]);
  const CODE_EVAL_GLOBALS = new Set(["eval", "Function"]);
  // process members that are interpreted; anything else (getBuiltinModule, binding, mainModule, ...) is UNVERIFIED.
  const SAFE_PROCESS_MEMBERS = new Set(["version", "versions", "platform", "arch", "browser", "title", "release", "pid", "ppid", "exitCode", "features",
    "nextTick", "hrtime", "cwd", "uptime", "memoryUsage", "cpuUsage", "emitWarning", "on", "once", "off", "removeListener"]);
  const TAINT_RE = /\bprocess\b|import\.meta|node:process|\brequire\s*\(\s*["']process["']|\beval\b|\bFunction\s*\(/;
  const LOCAL_DEP_RE = /(?:\brequire\s*\(|\bfrom\s+|\bimport\s*\(|\bimport\s+)\s*["'](\.{1,2}\/[^"']+)["']/g;
  const RESOLVE_EXTS = ["", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx", ".mts", ".cts", "/index.js", "/index.ts", "/index.tsx", "/index.mjs", "/index.cjs"];
  const resolveLocal = (fromFile, spec) => {
    const base = path.resolve(path.dirname(fromFile), spec);
    for (const ext of RESOLVE_EXTS) if (texts.has(base + ext)) return base + ext;
    return null;
  };
  // A scanned local module is tainted if it touches process / import.meta / eval, or (transitively) imports one that does.
  const taintCache = new Map();
  const fileTainted = (file, seen = new Set()) => {
    if (taintCache.has(file)) return taintCache.get(file);
    if (seen.has(file)) return false;
    seen.add(file);
    const text = texts.get(file) || "";
    let tainted = TAINT_RE.test(text);
    if (!tainted) for (const m of text.matchAll(LOCAL_DEP_RE)) { const dep = resolveLocal(file, m[1]); if (dep && fileTainted(dep, seen)) { tainted = true; break; } }
    taintCache.set(file, tainted);
    return tainted;
  };
  const isTaintedModule = (fromFile, spec) => {
    if (!spec || !spec.startsWith(".")) return false;
    const file = resolveLocal(fromFile, spec);
    return file ? fileTainted(file) : false;
  };
  const propNameOf = (el) => {
    const pn = el.propertyName || el.name;
    if (ts.isIdentifier(pn) || ts.isStringLiteral(pn) || ts.isNumericLiteral(pn)) return pn.text;
    if (ts.isComputedPropertyName(pn)) return literal(pn.expression);
    return null;
  };
  const texts = new Map(entries.map((e) => [path.resolve(e.abs), e.text]));
  const kindOf = (file) => { const ext = path.extname(file).toLowerCase(); return ext === ".tsx" ? ts.ScriptKind.TSX : (ext === ".ts" || ext === ".mts" || ext === ".cts") ? ts.ScriptKind.TS : ext === ".jsx" ? ts.ScriptKind.JSX : ts.ScriptKind.JS; };
  const options = { allowJs: true, noLib: true, noResolve: true, types: [], noEmit: true, jsx: ts.JsxEmit.Preserve };
  const host = ts.createCompilerHost(options, true);
  host.getSourceFile = (fn, lv) => { const t = texts.get(path.resolve(fn)); return t === undefined ? undefined : ts.createSourceFile(fn, t, lv, true, kindOf(fn)); };
  host.fileExists = (fn) => texts.has(path.resolve(fn));
  host.readFile = (fn) => texts.get(path.resolve(fn));
  const program = ts.createProgram([...texts.keys()], options, host);
  const checker = program.getTypeChecker();
  const isWrapper = (n) => !!n && (ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isNonNullExpression(n) || (ts.isTypeAssertionExpression && ts.isTypeAssertionExpression(n)) || (ts.isSatisfiesExpression && ts.isSatisfiesExpression(n)));
  const unwrap = (n) => { while (isWrapper(n)) n = n.expression; return n; };
  const useSite = (n) => { while (n.parent && isWrapper(n.parent) && n.parent.expression === n) n = n.parent; return n; };
  const literal = (n) => { n = unwrap(n); return n && (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) ? n.text : null; };
  const constString = (n) => { n = unwrap(n); if (!n) return null; const l0 = literal(n); if (l0 !== null) return l0; if (ts.isBinaryExpression(n) && n.operatorToken.kind === ts.SyntaxKind.PlusToken) { const l = constString(n.left), r = constString(n.right); return l !== null && r !== null ? l + r : null; } return null; };
  const isAmbient = (d) => d.getSourceFile().isDeclarationFile || (ts.getCombinedModifierFlags(d) & ts.ModifierFlags.Ambient) !== 0;
  const isRequireCall = (n) => { n = unwrap(n); return !!n && ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === "require" && n.arguments.length === 1; };
  const isRequireOfProcess = (n) => isRequireCall(n) && PROCESS_MODULES.has(literal(unwrap(n).arguments[0]) || "");
  const importDeclOf = (d) => { let p = d; while (p && !ts.isImportDeclaration(p)) p = p.parent; return p; };
  // What an identifier is bound to: "global" | "process" | "env" | "foreign" (import/require of another module) | "local"
  const classify = (id) => {
    const sym = checker.getSymbolAtLocation(id);
    if (!sym || !sym.declarations || sym.declarations.length === 0 || sym.declarations.every(isAmbient)) return "global";
    const d = sym.declarations[0];
    if (ts.isImportEqualsDeclaration(d)) {
      const ref = d.moduleReference;
      if (ts.isExternalModuleReference(ref)) return PROCESS_MODULES.has(literal(ref.expression) || "") ? "process" : "foreign";
      return "foreign"; // import x = Some.Entity: alias target not modelled
    }
    const imp = importDeclOf(d);
    if (imp) {
      if (!PROCESS_MODULES.has(literal(imp.moduleSpecifier) || "")) return "foreign";
      if (ts.isImportSpecifier(d)) { const imported = (d.propertyName || d.name).text; return imported === "env" ? "env" : imported === "default" ? "process" : "local"; }
      return "process";
    }
    let decl = d;
    if (ts.isBindingElement(d)) { while (decl && !ts.isVariableDeclaration(decl)) decl = decl.parent; }
    if (decl && ts.isVariableDeclaration(decl) && decl.initializer && isRequireCall(decl.initializer)) {
      if (!isRequireOfProcess(decl.initializer)) return "foreign";
      if (ts.isBindingElement(d)) return propNameOf(d) === "env" && ts.isIdentifier(d.name) ? "env" : "local";
      return "process";
    }
    return "local";
  };
  const isDeclName = (site) => { const p = site.parent; return !!p && (ts.isVariableDeclaration(p) || ts.isParameter(p) || ts.isBindingElement(p) || ts.isImportClause(p) || ts.isImportSpecifier(p) || ts.isNamespaceImport(p) || ts.isImportEqualsDeclaration(p) || ts.isFunctionDeclaration(p) || ts.isClassDeclaration(p)) && p.name === site; };
  // Identifier positions that are names, not references to a binding.
  const isReferencePosition = (n) => {
    const p = n.parent;
    if (!p) return true;
    if ((ts.isPropertyAccessExpression(p) || ts.isPropertyAssignment(p) || ts.isMethodDeclaration(p) || ts.isPropertyDeclaration(p) || ts.isJsxAttribute(p) || ts.isPropertySignature(p) || ts.isLabeledStatement(p) || ts.isGetAccessor(p) || ts.isSetAccessor(p)) && p.name === n) return false;
    if ((ts.isImportSpecifier(p) || ts.isExportSpecifier(p)) && p.propertyName === n) return false;
    if (ts.isQualifiedName(p) && p.right === n) return false;
    for (let a = p; a; a = a.parent) { if (ts.isTypeNode(a) && !ts.isTypeQueryNode(a)) return false; if (ts.isStatement(a)) break; }
    return true;
  };
  const isImportMeta = (n) => { n = unwrap(n); return !!n && ts.isMetaProperty(n) && n.keywordToken === ts.SyntaxKind.ImportKeyword && n.name.text === "meta"; };
  // A "root" is something whose members can reach the environment.
  const rootKind = (n) => {
    n = unwrap(n);
    if (!n) return null;
    if (isImportMeta(n)) return "import.meta";
    if (isRequireOfProcess(n)) return "process";
    if (ts.isIdentifier(n)) {
      if (!isReferencePosition(n)) return null;
      const c = classify(n);
      if (c === "global") return n.text === "process" ? "process" : GLOBAL_OBJECTS.has(n.text) ? "global-object" : null;
      if (c === "process" || c === "env") return c;
      return null;
    }
    return null;
  };
  // member name of an access: string for `.x` / ["x"] literal, null for computed
  const memberName = (p) => ts.isPropertyAccessExpression(p) ? p.name.text : literal(p.argumentExpression);
  // Module specifier a foreign binding came from (import / require / import-equals), or null.
  const foreignSpecOf = (id) => {
    const sym = checker.getSymbolAtLocation(id), d = sym && sym.declarations && sym.declarations[0];
    if (!d) return null;
    if (ts.isImportEqualsDeclaration(d) && ts.isExternalModuleReference(d.moduleReference)) return literal(d.moduleReference.expression);
    const imp = importDeclOf(d);
    if (imp) return literal(imp.moduleSpecifier);
    let decl = d;
    if (ts.isBindingElement(d)) { while (decl && !ts.isVariableDeclaration(decl)) decl = decl.parent; }
    if (decl && ts.isVariableDeclaration(decl) && decl.initializer && isRequireCall(decl.initializer)) return literal(unwrap(decl.initializer).arguments[0]);
    return null;
  };
  function analyzeFile(abs) {
    const sf = program.getSourceFile(path.resolve(abs));
    const lineOf = (node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
    let isClient = false;
    for (const st of sf.statements) { if (ts.isExpressionStatement(st) && ts.isStringLiteral(st.expression)) { if (st.expression.text === "use client") isClient = true; continue; } break; }
    const refs = [], unverified = [];
    const flag = (node, reason) => unverified.push({ line: lineOf(node), reason });
    const readEnvObject = (envNode) => {
      // envNode is an expression that IS the env object; classify its use site.
      const site = useSite(envNode), p = site.parent;
      if (p && (ts.isPropertyAccessExpression(p) || ts.isElementAccessExpression(p)) && p.expression === site) {
        const k = ts.isPropertyAccessExpression(p) ? p.name.text : constString(p.argumentExpression);
        if (k !== null) refs.push({ key: k, line: lineOf(p) }); else flag(p, "computed env key");
        return;
      }
      if (p && ts.isVariableDeclaration(p) && p.initializer === site && ts.isObjectBindingPattern(p.name)) {
        for (const el of p.name.elements) {
          if (el.dotDotDotToken) { flag(el, "rest spread of env"); continue; }
          const prop = el.propertyName || el.name; let k = null;
          if (prop && (ts.isIdentifier(prop) || ts.isStringLiteral(prop))) k = prop.text; else if (prop && ts.isComputedPropertyName(prop)) k = constString(prop.expression);
          if (k !== null) refs.push({ key: k, line: lineOf(el) }); else flag(el, "computed env destructuring key");
        }
        return;
      }
      flag(site, "env object used other than a literal-key read");
    };
    const visit = (node) => {
      // Named imports from the process module follow the same member allowlist as require().
      if (ts.isImportDeclaration(node) && PROCESS_MODULES.has(literal(node.moduleSpecifier) || "")) {
        const nb = node.importClause && node.importClause.namedBindings;
        if (nb && ts.isNamedImports(nb)) for (const sp of nb.elements) {
          const imported = (sp.propertyName || sp.name).text;
          if (imported !== "env" && imported !== "default" && (!SAFE_PROCESS_MEMBERS.has(imported) || PROCESS_CHAINING.has(imported))) flag(sp, `process member '${imported}' not in the allowlist`);
        }
      }
      // Dynamic import of the process module, of a computed specifier, or of inline code (data:/http:).
      if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
        const spec = node.arguments.length ? literal(node.arguments[0]) : null;
        if (spec === null || PROCESS_MODULES.has(spec) || /^(?:data|https?|blob):/i.test(spec)) flag(node, "dynamic import of the process module, inline code or a computed specifier");
      }
      // require() with a computed specifier.
      if (isRequireCall(node) && literal(unwrap(node).arguments[0]) === null) flag(node, "require with a computed specifier");
      if (ts.isIdentifier(node) && !isDeclName(node) && isReferencePosition(node)) {
        const c = classify(node);
        const site = useSite(node), p = site.parent;
        const accessed = p && (ts.isPropertyAccessExpression(p) || ts.isElementAccessExpression(p)) && p.expression === site;
        const typeOnly = p && (ts.isTypeOfExpression(p) || ts.isTypeQueryNode(p) || (ts.isPropertyAccessExpression(p) && p.name === site));
        if (c === "foreign" && accessed && memberName(p) === "env") flag(p, "env read through a binding imported from another module");
        if (c === "foreign" && !typeOnly && !isDeclName(site) && isTaintedModule(path.resolve(abs), foreignSpecOf(node))) {
          flag(site, "binding from a local module that (transitively) touches process: its value is not certified");
        }
        if (c === "global" && CODE_EVAL_GLOBALS.has(node.text) && !typeOnly) flag(site, `${node.text} executes code that is not analysed`);
        if (c === "global" && (node.text === "setTimeout" || node.text === "setInterval") && p && ts.isCallExpression(p) && p.expression === site && p.arguments.length && literal(p.arguments[0]) !== null) flag(p, "string-evaluated timer callback");
        if (c === "global" && node.text === "require") {
          const isCallee = p && ts.isCallExpression(p) && p.expression === site;
          if (!isCallee && !typeOnly) flag(site, "require used as a value");
        }
        if (c === "global" && node.text === "module" && accessed && ["require", "constructor", "children", "parent", "paths"].includes(memberName(p) || "require")) flag(p, "module loader reached through module");
        if (c === "foreign" && p && ts.isImportSpecifier(node.parent)) { /* declaration */ }
        if (c === "foreign") {
          const sym = checker.getSymbolAtLocation(node), d = sym && sym.declarations && sym.declarations[0];
          if (d && ts.isImportSpecifier(d) && ["env", "process"].includes((d.propertyName || d.name).text) && !typeOnly) flag(site, "env/process imported from another module");
        }
      }
      if (!isWrapper(node)) {
        const kind = rootKind(node);
        if (kind === "env") { if (!isDeclName(useSite(node))) readEnvObject(node); return; }
        if (kind) {
          const site = useSite(node), p = site.parent;
          // Static binding of the process module: const p = require("process") / const {env, version} = require("process")
          if (kind === "process" && isRequireOfProcess(node) && p && ts.isVariableDeclaration(p) && p.initializer === site) {
            if (ts.isObjectBindingPattern(p.name)) for (const el of p.name.elements) {
              if (el.dotDotDotToken) { flag(el, "rest spread of the process module"); continue; }
              const n = propNameOf(el);
              if (n === null) { flag(el, "computed member of process"); continue; }
              if (n === "env") {
                if (ts.isIdentifier(el.name)) continue; // classify() maps this binding to env
                if (ts.isObjectBindingPattern(el.name)) {
                  for (const e2 of el.name.elements) {
                    const k = e2.dotDotDotToken ? null : propNameOf(e2);
                    if (k === null || !ts.isIdentifier(e2.name)) flag(e2, "env destructuring form not in the allowlist"); else refs.push({ key: k, line: lineOf(e2) });
                  }
                  continue;
                }
                flag(el, "env destructuring form not in the allowlist");
                continue;
              }
              if (!SAFE_PROCESS_MEMBERS.has(n) || PROCESS_CHAINING.has(n)) flag(el, `process member '${n}' not in the allowlist`);
            } else if (!ts.isIdentifier(p.name)) flag(site, "process module bound with an unsupported pattern");
            return;
          }
          const accessed = p && (ts.isPropertyAccessExpression(p) || ts.isElementAccessExpression(p)) && p.expression === site;
          const typeOnly = p && (ts.isTypeOfExpression(p) || ts.isTypeQueryNode(p));
          if (isDeclName(site) || typeOnly) { /* declaration or typeof guard */ }
          else if (!accessed) flag(site, `${kind} object escapes (alias, argument, spread, return)`);
          else {
            const name = memberName(p);
            if (name === null) flag(p, `computed member of ${kind}`);
            else if ((kind === "process" || kind === "import.meta") && name === "env") { readEnvObject(p); return; }
            else if (kind === "process" && !SAFE_PROCESS_MEMBERS.has(name)) flag(p, `process member '${name}' not in the allowlist`);
            else if (kind === "process" && PROCESS_CHAINING.has(name)) {
              const call = p.parent && ts.isCallExpression(p.parent) && p.parent.expression === p ? p.parent : null;
              if (!call || !(call.parent && ts.isExpressionStatement(call.parent))) flag(p, `return value of process.${name}() (the process object) is used`);
            }
            else if (kind === "global-object" && GLOBAL_ESCAPE_MEMBERS.has(name)) flag(p, `'${name}' reached through a global object`);
          }
          if (ts.isCallExpression(unwrap(node))) return; // require("process") handled as a whole
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
    return { isClient, refs, unverified, dynamic: unverified, parseDiagnostics: (sf.parseDiagnostics || []).length };
  }
  return { analyzeFile };
}
// END-SHARED-ENV-ANALYZER

function resolveTypescript(root) {
  const candidates = [];
  if (process.env.DELTAHARNESS_TYPESCRIPT) candidates.push(process.env.DELTAHARNESS_TYPESCRIPT);
  try { candidates.push(require.resolve("typescript", { paths: [root] })); } catch (_) { /* not installed in project */ }
  for (const c of candidates) {
    try { return require(c); } catch (_) { /* try next */ }
  }
  return null;
}

function main() {
  const root = path.resolve(process.argv[2] || ".");
  const ts = resolveTypescript(root);
  if (!ts) {
    process.stdout.write(JSON.stringify({ mode: "unavailable", reason: "typescript not resolvable from project or DELTAHARNESS_TYPESCRIPT" }));
    process.exit(3);
  }
  const files = JSON.parse(fs.readFileSync(0, "utf8") || "[]");
  const entries = [], out = {};
  for (const rel of files) {
    try { entries.push({ rel, abs: path.join(root, rel), text: fs.readFileSync(path.join(root, rel), "utf8") }); }
    catch (err) { out[rel] = { error: String(err && err.message || err).slice(0, 200) }; }
  }
  const analyzer = createEnvAnalyzer(ts, entries);
  for (const e of entries) {
    try { out[e.rel] = analyzer.analyzeFile(e.abs); }
    catch (err) { out[e.rel] = { error: String(err && err.message || err).slice(0, 200) }; }
  }
  process.stdout.write(JSON.stringify({ mode: "typescript-ast", typescriptVersion: ts.version, files: out }));
}

if (require.main === module) main();
module.exports = { createEnvAnalyzer, resolveTypescript };
