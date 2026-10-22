#!/usr/bin/env node
/*
 * Runs the frozen KCI engine and prints its outputs as JSON.
 *
 * The files beside this one are byte-for-byte slices of KCI's published
 * index.html (see SOURCE.json). Nothing in them is edited. This harness only
 * loads them and calls them the same way the KCI app does, with the app's
 * default settings.
 *
 * Usage: node legacy/kci/harness.js > out.json
 */
'use strict';
const fs = require('fs');
const path = require('path');

const here = __dirname;
const K = require(path.join(here, 'engine.js'));
const seed = JSON.parse(fs.readFileSync(path.join(here, 'seed.json'), 'utf8'));

// The app's model-setup and self-test code are closures inside the app script,
// where `K` and `isNum` are in scope. Recreate exactly that scope.
const setupSrc = fs.readFileSync(path.join(here, 'model_setup.js'), 'utf8');
const casesSrc = fs.readFileSync(path.join(here, 'controlled_cases.js'), 'utf8');
const app = new Function(
  'K', 'isNum',
  setupSrc + '\n' + casesSrc +
  '\nreturn { defaultLBO, defaultDCF, defaultScenarios, fitYears, fitDcf, dcfWacc, analyse, controlledCases };'
)(K, K.isNum);

// The app's DEFAULT_SETTINGS (index.html, "const DEFAULT_SETTINGS") as used by analyse().
const settings = {
  scoring: JSON.parse(JSON.stringify(K.DEFAULT_SCORING)),
  criteria: JSON.parse(JSON.stringify(K.DEFAULT_CRITERIA)),
};

const pick = (r) => (r && r.ok ? {
  irr: r.returns.irr, moic: r.returns.moic, entryLeverage: r.entry.leverage,
  sponsorEquity: r.entry.sponsorEquity, exitEquity: r.exit.exitEquityRaw,
  exitNetDebt: r.exit.exitNetDebt, cures: r.returns.cures, allChecksPass: r.allChecksPass,
} : { ok: false, errors: r ? r.errors : ['no result'] });

const companies = Object.values(seed.deals)
  .sort((a, b) => a.id.localeCompare(b.id))
  .map((d) => {
    const a = app.analyse(d, settings);
    return {
      id: d.id, ticker: d.ticker, dataStatus: d.dataStatus, customModel: !!d.lbo,
      lbo: pick(a.lbo),
      downside: pick(a.scenarios.downside.result),
      base: pick(a.scenarios.base.result),
      upside: pick(a.scenarios.upside.result),
      dcf: a.dcf.ok ? { wacc: a.wacc.value, ev: a.dcf.primary.ev, equity: a.dcf.primary.equity,
        perShare: a.dcf.primary.perShare, tvShareOfEV: a.dcf.primary.tvShareOfEV } : { ok: false, errors: a.dcf.errors },
      score: a.score.total,
      decision: a.rec.decision,
    };
  });

const dbx = seed.deals['us-dbx'];
const dbxLbo = K.runLBO(dbx.lbo);
const maxMult = K.maxEntryMultiple(dbx.lbo, 0.2);
const dbxDcf = K.runDCF(dbx.dcf);
const dropbox = {
  irr: dbxLbo.returns.irr,
  moic: dbxLbo.returns.moic,
  allChecksPass: dbxLbo.allChecksPass,
  unleveredIrr: K.runLBO({ ...dbx.lbo, tranches: dbx.lbo.tranches.map((t) => ({ ...t, leverage: 0 })) }).returns.irr,
  maxEntryMultipleAt20: maxMult,
  maxPricePerShareAt20: (maxMult * dbx.lbo.entryEbitda - dbx.netDebt) / dbx.market.sharesM,
  dcfPerShare: dbxDcf.primary.perShare,
  sponsorCashFlows: dbxLbo.returns.sponsorCashFlows,
};

const cases = app.controlledCases().map((c) => ({ area: c.area, name: c.name, ok: c.ok, expected: c.expected, actual: c.actual }));

process.stdout.write(JSON.stringify({ engine: 'kci-v1', controlledCases: cases, dropbox, companies }, null, 1) + '\n');
