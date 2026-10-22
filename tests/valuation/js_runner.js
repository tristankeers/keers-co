#!/usr/bin/env node
/*
 * Runs a batch of calls against the frozen KCI JavaScript engine (legacy/kci/engine.js).
 * Reads [{fn, args}] as JSON on stdin and writes the list of results as JSON on stdout.
 * Used only by the parity tests.
 */
'use strict';
const path = require('path');
const K = require(path.join(__dirname, '..', '..', 'legacy', 'kci', 'engine.js'));

const CALLS = {
  irr: (a) => K.irr(a.cfs),
  npv: (a) => K.npv(a.rate, a.cfs),
  stats: (a) => K.stats(a.values),
  evBridge: (a) => K.evBridge(a),
  wacc: (a) => K.wacc(a),
  runDCF: (a) => K.runDCF(a),
  runLBO: (a) => K.runLBO(a),
  breakEven: (a) => K.breakEven(K.runLBO(a.lbo), a.target),
  maxEntryMultiple: (a) => K.maxEntryMultiple(a.lbo, a.target),
  applyScenario: (a) => K.applyScenario(a.base, a.sc),
  chartSeries: (a) => K.chartSeries(K.runLBO(a.lbo)),
  compsAnalysis: (a) => K.compsAnalysis(a.companies, a.target ? { target: a.target } : {}),
  scoreTarget: (a) => K.scoreTarget(a.deal, a.cfg || K.DEFAULT_SCORING),
  passesFilters: (a) => K.passesFilters(a.deal, a.filters),
  recommend: (a) => K.recommend(a.m, a.c || K.DEFAULT_CRITERIA),
};

let input = '';
process.stdin.on('data', (c) => { input += c; });
process.stdin.on('end', () => {
  const calls = JSON.parse(input);
  const out = calls.map(({ fn, args }) => {
    try { return { ok: true, value: CALLS[fn](args) }; } catch (e) { return { ok: false, error: String(e) }; }
  });
  // JSON turns NaN and Infinity into null; the Python side normalises the same way.
  process.stdout.write(JSON.stringify(out));
});
