/* Keers Capital Intelligence — financial calculation engine.
 * Pure functions, no DOM. Units: currency amounts in millions unless stated;
 * rates and percentages as decimals (0.25 = 25%). `null` means unknown;
 * 0 means a known zero. Every function returns explicit errors rather than NaN.
 */
(function (root) {
  'use strict';

  // ---------- primitives ----------
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const num = (v) => (isNum(v) ? v : null);
  const safeDiv = (a, b) => (isNum(a) && isNum(b) && b !== 0 ? a / b : null);
  const sum = (arr) => arr.reduce((s, v) => s + (isNum(v) ? v : 0), 0);
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const EPS = 1e-6;
  const at = (x, i) => {
    if (Array.isArray(x)) return x.length ? x[Math.min(i, x.length - 1)] : null;
    return x;
  };

  // ---------- time value ----------
  function npv(rate, cfs, times) {
    let s = 0;
    for (let i = 0; i < cfs.length; i++) {
      const t = times ? times[i] : i;
      s += cfs[i] / Math.pow(1 + rate, t);
    }
    return s;
  }

  /* IRR by bracketed bisection on NPV(r) = 0. Robust for any cash-flow
   * pattern with a sign change; returns {value, note}. Annual periods. */
  function irr(cfs, times) {
    if (!Array.isArray(cfs) || cfs.length < 2 || !cfs.every(isNum)) {
      return { value: null, note: 'Cash flows incomplete' };
    }
    const hasNeg = cfs.some((c) => c < -EPS);
    const hasPos = cfs.some((c) => c > EPS);
    if (!hasNeg) return { value: null, note: 'No equity invested' };
    if (!hasPos) return { value: -1, note: 'Total loss: no equity proceeds' };
    const f = (r) => npv(r, cfs, times);
    let lo = -0.999999, hi = 1.0;
    let flo = f(lo), fhi = f(hi);
    let guard = 0;
    while (Math.sign(flo) === Math.sign(fhi) && guard < 60) {
      hi = hi * 2 + 1; fhi = f(hi); guard++;
      if (hi > 1e6) break;
    }
    if (Math.sign(flo) === Math.sign(fhi)) return { value: null, note: 'IRR could not be bracketed' };
    for (let i = 0; i < 300; i++) {
      const mid = (lo + hi) / 2;
      const fm = f(mid);
      if (Math.abs(fm) < 1e-10 || (hi - lo) / 2 < 1e-12) return { value: mid, note: null };
      if (Math.sign(fm) === Math.sign(flo)) { lo = mid; flo = fm; } else { hi = mid; fhi = fm; }
    }
    return { value: (lo + hi) / 2, note: null };
  }

  // ---------- statistics ----------
  function quantile(sorted, p) {
    // Linear interpolation, same as Excel QUARTILE.INC / PERCENTILE.INC
    if (!sorted.length) return null;
    const pos = (sorted.length - 1) * p;
    const lo = Math.floor(pos), hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }
  function stats(values) {
    const v = values.filter(isNum).slice().sort((a, b) => a - b);
    if (!v.length) return { n: 0, mean: null, median: null, q1: null, q3: null, min: null, max: null, iqr: null, lowerFence: null, upperFence: null };
    const q1 = quantile(v, 0.25), q3 = quantile(v, 0.75), iqr = q3 - q1;
    return {
      n: v.length, mean: sum(v) / v.length, median: quantile(v, 0.5), q1, q3,
      min: v[0], max: v[v.length - 1], iqr, lowerFence: q1 - 1.5 * iqr, upperFence: q3 + 1.5 * iqr,
    };
  }

  // ---------- valuation bridge ----------
  function evBridge({ ebitda, multiple, netDebt, otherClaims = 0, nonOperatingAssets = 0 }) {
    const errors = [];
    if (!isNum(ebitda)) errors.push('EBITDA is required');
    if (!isNum(multiple)) errors.push('EV/EBITDA multiple is required');
    if (!isNum(netDebt)) errors.push('Net debt is required (enter 0 if the business has none)');
    if (errors.length) return { ok: false, errors };
    const ev = ebitda * multiple;
    const equity = ev - netDebt - (otherClaims || 0) + (nonOperatingAssets || 0);
    return { ok: true, errors: [], ev, equity, netDebt, otherClaims: otherClaims || 0, nonOperatingAssets: nonOperatingAssets || 0,
      warnings: ebitda <= 0 ? ['EBITDA is not positive; an EV/EBITDA valuation is not meaningful'] : [] };
  }

  // ---------- comparable companies ----------
  const MULTIPLES = [
    { key: 'evRevenue', label: 'EV / Revenue', num: 'ev', den: 'revenue' },
    { key: 'evEbitda', label: 'EV / EBITDA', num: 'ev', den: 'ebitda' },
    { key: 'evEbit', label: 'EV / EBIT', num: 'ev', den: 'ebit' },
    { key: 'pe', label: 'P / E', num: 'marketCap', den: 'netIncome' },
  ];
  function compMultiples(c) {
    const out = {};
    for (const m of MULTIPLES) {
      const n = num(c[m.num]), d = num(c[m.den]);
      if (n === null || d === null) out[m.key] = { value: null, status: 'missing' };
      else if (d <= 0) out[m.key] = { value: null, status: 'nm' }; // not meaningful
      else out[m.key] = { value: n / d, status: 'ok' };
    }
    return out;
  }
  function compsAnalysis(companies, opts = {}) {
    const included = companies.filter((c) => c.include !== false);
    const rows = companies.map((c) => ({ ...c, multiples: compMultiples(c) }));
    const summary = {};
    for (const m of MULTIPLES) {
      const vals = rows.filter((r) => r.include !== false).map((r) => r.multiples[m.key].value).filter(isNum);
      const s = stats(vals);
      summary[m.key] = s;
      rows.forEach((r) => {
        const v = r.multiples[m.key].value;
        r.multiples[m.key].outlier = isNum(v) && s.n >= 4 && (v < s.lowerFence || v > s.upperFence);
      });
    }
    const missing = rows.filter((r) => r.include !== false).reduce((acc, r) => {
      for (const m of MULTIPLES) if (r.multiples[m.key].status !== 'ok') acc.push({ name: r.name, multiple: m.label, status: r.multiples[m.key].status });
      return acc;
    }, []);
    let implied = null;
    const t = opts.target;
    if (t) {
      implied = {};
      for (const m of MULTIPLES) {
        const base = m.den === 'netIncome' ? num(t.netIncome) : num(t[m.den]);
        const s = summary[m.key];
        if (base === null || base <= 0 || !s.n) { implied[m.key] = null; continue; }
        const isEquity = m.key === 'pe';
        const toEV = (mult) => (isEquity ? mult * base + (num(t.netDebt) || 0) : mult * base);
        implied[m.key] = { low: toEV(s.q1), mid: toEV(s.median), high: toEV(s.q3), basis: isEquity ? 'Equity value + net debt' : 'Enterprise value' };
      }
    }
    return { rows, summary, missing, implied, includedCount: included.length };
  }

  // ---------- WACC ----------
  function wacc({ riskFree, beta, erp, sizePremium = 0, preTaxCostOfDebt, taxRate, debtWeight }) {
    const errors = [];
    [['Risk-free rate', riskFree], ['Beta', beta], ['Equity risk premium', erp], ['Pre-tax cost of debt', preTaxCostOfDebt], ['Tax rate', taxRate], ['Debt weight', debtWeight]]
      .forEach(([l, v]) => { if (!isNum(v)) errors.push(l + ' is required'); });
    if (errors.length) return { ok: false, errors };
    if (debtWeight < 0 || debtWeight >= 1) return { ok: false, errors: ['Debt weight must be between 0% and 99%'] };
    const costOfEquity = riskFree + beta * erp + (sizePremium || 0);
    const afterTaxCostOfDebt = preTaxCostOfDebt * (1 - taxRate);
    const value = (1 - debtWeight) * costOfEquity + debtWeight * afterTaxCostOfDebt;
    return { ok: true, errors: [], costOfEquity, afterTaxCostOfDebt, equityWeight: 1 - debtWeight, debtWeight, value };
  }

  // ---------- DCF ----------
  /* Unlevered FCF = EBIT × (1 − t) + D&A − Capex − ΔNWC, with tax floored at
   * zero when EBIT is negative (no tax credit assumed). ΔNWC = NWC% × ΔRevenue.
   * Terminal value is valued at the end of the final year under both methods.
   * Mid-year convention discounts each year's FCF at (t − 0.5). */
  function runDCF(inp) {
    const errors = [], warnings = [];
    const N = inp.years;
    if (!Number.isInteger(N) || N < 1 || N > 15) errors.push('Projection period must be 1 to 15 years');
    if (!isNum(inp.baseRevenue) || inp.baseRevenue <= 0) errors.push('Base-year revenue must be positive');
    if (!isNum(inp.wacc) || inp.wacc <= 0) errors.push('WACC must be positive');
    if (!isNum(inp.taxRate) || inp.taxRate < 0 || inp.taxRate >= 1) errors.push('Tax rate must be between 0% and 99%');
    const method = inp.terminalMethod || 'gordon';
    if ((method === 'gordon' || method === 'both')) {
      if (!isNum(inp.terminalGrowth)) errors.push('Terminal growth rate is required');
      else if (isNum(inp.wacc) && inp.terminalGrowth >= inp.wacc) errors.push('Terminal growth must be below WACC');
    }
    if ((method === 'exit' || method === 'both') && (!isNum(inp.exitMultiple) || inp.exitMultiple <= 0)) errors.push('Exit multiple must be positive');
    if (Number.isInteger(N)) for (let i = 0; i < N; i++) {
      if (!isNum(at(inp.revenueGrowth, i))) { errors.push('Revenue growth is missing for year ' + (i + 1)); break; }
      const mg = at(inp.ebitdaMargin, i);
      if (!isNum(mg) || mg <= -1 || mg > 1) { errors.push('EBITDA margin for year ' + (i + 1) + ' must be a number between -100% and 100%'); break; }
    }
    if (errors.length) return { ok: false, errors, warnings };

    const rows = [];
    let prevRev = inp.baseRevenue;
    for (let i = 0; i < N; i++) {
      const g = at(inp.revenueGrowth, i) ?? 0;
      const revenue = prevRev * (1 + g);
      const margin = at(inp.ebitdaMargin, i) ?? 0;
      const ebitda = revenue * margin;
      const da = revenue * (at(inp.daPct, i) ?? 0);
      const ebit = ebitda - da;
      const tax = Math.max(0, ebit * inp.taxRate);
      const nopat = ebit - tax;
      const capex = revenue * (at(inp.capexPct, i) ?? 0);
      const dNwc = (revenue - prevRev) * (at(inp.nwcPct, i) ?? 0);
      const ufcf = nopat + da - capex - dNwc;
      const t = i + 1;
      const discT = inp.midYear ? t - 0.5 : t;
      const df = 1 / Math.pow(1 + inp.wacc, discT);
      rows.push({ year: t, revenue, growth: g, ebitda, margin, da, ebit, tax, nopat, capex, dNwc, ufcf, discountPeriod: discT, discountFactor: df, pv: ufcf * df });
      prevRev = revenue;
    }
    const sumPV = sum(rows.map((r) => r.pv));
    const last = rows[N - 1];
    // Terminal value timing. Exit multiple: a sale at the end of year N, discounted N periods.
    // Perpetuity: with the mid-year convention the perpetuity's cash flows also arrive mid-year, so its
    // value is discounted N - 0.5 periods; with end-year flows, N periods.
    const W = inp.wacc, half = inp.midYear ? Math.pow(1 + W, 0.5) : 1;
    const dfExit = 1 / Math.pow(1 + W, N), dfGordon = dfExit * half;
    const out = { ok: true, errors, warnings, rows, sumPV, method, terminal: {} };
    const netDebtKnown = isNum(inp.netDebt);
    if (!netDebtKnown) warnings.push('Net debt is unknown, so equity value and value per share cannot be calculated');
    const claims = (netDebtKnown ? inp.netDebt : 0) + (num(inp.otherClaims) || 0) - (num(inp.nonOperatingAssets) || 0);
    const build = (tv, df) => {
      const pvTV = tv * df;
      const ev = sumPV + pvTV;
      const equity = netDebtKnown ? ev - claims : null;
      const perShare = isNum(equity) && isNum(inp.sharesOutstanding) && inp.sharesOutstanding > 0 ? equity / inp.sharesOutstanding : null;
      return { tv, pvTV, discountFactor: df, ev, equity, perShare, tvShareOfEV: safeDiv(pvTV, ev) };
    };
    if (method === 'gordon' || method === 'both') {
      const tv = last.ufcf * (1 + inp.terminalGrowth) / (W - inp.terminalGrowth);
      // value of the perpetuity at the end of year N, for comparison with an exit multiple
      out.terminal.gordon = { ...build(tv, dfGordon), impliedExitMultiple: last.ebitda > 0 ? (tv * half) / last.ebitda : null };
      if (last.ufcf <= 0) warnings.push('Final-year free cash flow is not positive; the perpetuity value is not meaningful');
    }
    if (method === 'exit' || method === 'both') {
      const tv = last.ebitda * inp.exitMultiple;
      // implied perpetual growth: perpetuity value at end of year N equals the exit value.
      // FCF(1+g)/(W-g) = TV / half  =>  g = (T*W - FCF)/(T + FCF), with T = TV / half
      const T = tv / half;
      const g = safeDiv(T * W - last.ufcf, T + last.ufcf);
      out.terminal.exit = { ...build(tv, dfExit), impliedPerpetualGrowth: g };
      if (last.ebitda <= 0) warnings.push('Final-year EBITDA is not positive; an exit-multiple terminal value is not meaningful');
    }
    out.primary = out.terminal[method === 'exit' ? 'exit' : 'gordon'];
    out.claims = { netDebt: netDebtKnown ? inp.netDebt : null, otherClaims: num(inp.otherClaims) || 0, nonOperatingAssets: num(inp.nonOperatingAssets) || 0 };
    if (out.primary.tvShareOfEV > 0.85) warnings.push('Terminal value is over 85% of enterprise value; the result is highly sensitive to terminal assumptions');
    return out;
  }

  // ---------- LBO ----------
  /* Conventions (shown in the app):
   * - Annual model; entry at t = 0, cash flows at year ends, exit at end of year N.
   * - Cash-free, debt-free purchase: uses = entry EV + fees + cash funded to balance sheet.
   * - Interest is charged on opening balances (no circularity). Interest income on opening cash.
   * - Tax = max(0, EBT − loss carry-forward used) × rate; losses carry forward indefinitely.
   * - Order of cash each year: operating FCF → mandatory amortisation (tranche order) →
   *   if cash < minimum, draw revolver up to commitment → if cash still < 0, sponsor equity
   *   cure (recorded as a negative sponsor cash flow) → excess over minimum repays revolver,
   *   then the sweep % of remaining excess prepays sweep-eligible tranches in order.
   * - Equity cures do not change ownership percentages.
   * - Exit equity is floored at zero (limited liability). */
  function validateLBO(inp) {
    const e = [];
    if (!isNum(inp.entryEbitda) || inp.entryEbitda <= 0) e.push('Entry EBITDA must be positive');
    if (!isNum(inp.entryRevenue) || inp.entryRevenue <= 0) e.push('Entry revenue must be positive');
    if (isNum(inp.entryRevenue) && isNum(inp.entryEbitda) && inp.entryEbitda > inp.entryRevenue) e.push('Entry EBITDA cannot exceed revenue');
    const evGiven = isNum(inp.entryEVOverride) && inp.entryEVOverride > 0;
    if (!evGiven && (!isNum(inp.entryMultiple) || inp.entryMultiple <= 0)) e.push('Entry multiple must be positive');
    if (!Number.isInteger(inp.holdYears) || inp.holdYears < 1 || inp.holdYears > 10) e.push('Holding period must be a whole number of years from 1 to 10');
    if (!isNum(inp.exitMultiple) || inp.exitMultiple <= 0) e.push('Exit multiple must be positive');
    if (!isNum(inp.taxRate) || inp.taxRate < 0 || inp.taxRate >= 1) e.push('Tax rate must be between 0% and 99%');
    if (!Array.isArray(inp.tranches)) e.push('Debt tranches are required');
    else inp.tranches.forEach((t, i) => {
      const n = t.name || 'Tranche ' + (i + 1);
      if (!isNum(t.leverage) || t.leverage < 0) e.push(n + ': leverage must be zero or more');
      if (!isNum(t.rate) || t.rate < 0) e.push(n + ': interest rate must be zero or more');
      if (!isNum(t.amortPct) || t.amortPct < 0 || t.amortPct > 1) e.push(n + ': amortisation must be 0% to 100% a year');
    });
    if (isNum(inp.cashSweepPct) && (inp.cashSweepPct < 0 || inp.cashSweepPct > 1)) e.push('Cash sweep must be 0% to 100%');
    if (isNum(inp.minCash) && inp.minCash < 0) e.push('Minimum cash cannot be negative');
    if (isNum(inp.rollover) && inp.rollover < 0) e.push('Management rollover cannot be negative');
    if (Number.isInteger(inp.holdYears)) for (let i = 0; i < inp.holdYears; i++) {
      if (!isNum(at(inp.revenueGrowth, i))) { e.push('Revenue growth is missing for year ' + (i + 1)); break; }
      const mg = at(inp.ebitdaMargin, i);
      if (!isNum(mg) || mg <= -1 || mg > 1) { e.push('EBITDA margin for year ' + (i + 1) + ' must be a number between -100% and 100%'); break; }
    }
    ['daPct', 'capexPct', 'nwcPct'].forEach((k) => { if (inp[k] !== undefined && inp[k] !== null && !Array.isArray(inp[k]) && !isNum(inp[k])) e.push(k + ' must be a number'); });
    return e;
  }

  function runLBO(raw) {
    const inp = { cashSweepPct: 1, minCash: 0, rollover: 0, transactionFeePct: 0, financingFeePct: 0, exitFeePct: 0,
      existingDebt: 0, existingCash: 0, interestOnCashRate: 0, daPct: 0, capexPct: 0, nwcPct: 0,
      revolver: { commitment: 0, rate: 0 }, ...raw };
    const errors = validateLBO(inp);
    if (errors.length) return { ok: false, errors, warnings: [] };
    const warnings = [];
    const N = inp.holdYears;

    // Entry
    const entryEV = isNum(inp.entryEVOverride) && inp.entryEVOverride > 0 ? inp.entryEVOverride : inp.entryEbitda * inp.entryMultiple;
    const entryMultipleEff = entryEV / inp.entryEbitda;
    const tranches = inp.tranches.map((t, i) => ({ ...t, name: t.name || 'Tranche ' + (i + 1), amount: t.leverage * inp.entryEbitda }));
    const newDebt = sum(tranches.map((t) => t.amount));
    const transactionFees = entryEV * inp.transactionFeePct;
    const financingFees = newDebt * inp.financingFeePct;
    const existingNetDebt = (inp.existingDebt || 0) - (inp.existingCash || 0);
    const equityPurchasePrice = entryEV - existingNetDebt;
    const uses = [
      { label: 'Purchase of equity', value: equityPurchasePrice },
      { label: 'Refinance existing debt', value: inp.existingDebt || 0 },
      { label: 'Less: cash acquired with the business', value: -(inp.existingCash || 0) },
      { label: 'Transaction fees', value: transactionFees },
      { label: 'Financing fees', value: financingFees },
      { label: 'Cash to balance sheet', value: inp.minCash },
    ];
    const totalUses = sum(uses.map((u) => u.value));
    const sponsorEquity = totalUses - newDebt - inp.rollover;
    const sources = [
      ...tranches.map((t) => ({ label: t.name, value: t.amount, multiple: t.leverage })),
      { label: 'Management rollover', value: inp.rollover },
      { label: 'Sponsor equity', value: sponsorEquity },
    ];
    const totalSources = sum(sources.map((s) => s.value));
    if (equityPurchasePrice < 0) errors.push('Existing net debt exceeds the purchase enterprise value');
    if (sponsorEquity <= 0) errors.push('Debt and rollover fund all uses, so sponsor equity is zero or negative. Reduce leverage.');
    if (errors.length) return { ok: false, errors, warnings, entry: { entryEV, newDebt, totalUses } };
    const totalEquityIn = sponsorEquity + inp.rollover;
    const sponsorShare = sponsorEquity / totalEquityIn;
    if (newDebt / inp.entryEbitda > 7) warnings.push('Entry leverage above 7.0x EBITDA is aggressive for lower-middle-market credit');

    // Projection loop
    const bal = tranches.map((t) => t.amount);
    let revolver = 0;
    let cash = inp.minCash;
    let nol = 0;
    let prevRev = inp.entryRevenue;
    const years = [];
    const checks = [];
    for (let i = 0; i < N; i++) {
      const y = i + 1;
      const g = at(inp.revenueGrowth, i) ?? 0;
      const revenue = prevRev * (1 + g);
      const margin = at(inp.ebitdaMargin, i);
      const ebitda = revenue * margin;
      const da = revenue * (at(inp.daPct, i) ?? 0);
      const ebit = ebitda - da;
      const openBal = bal.slice();
      const openRevolver = revolver;
      const openCash = cash;
      const trancheInterest = tranches.map((t, k) => openBal[k] * (t.rate + (inp.rateShift || 0)));
      const revolverInterest = openRevolver * ((inp.revolver.rate || 0) + (inp.rateShift || 0));
      const interestExpense = sum(trancheInterest) + revolverInterest;
      const interestIncome = Math.max(0, openCash) * (inp.interestOnCashRate || 0);
      const ebt = ebit - interestExpense + interestIncome;
      let taxable = ebt, nolUsed = 0;
      if (ebt < 0) { nol += -ebt; taxable = 0; }
      else { nolUsed = Math.min(nol, ebt); nol -= nolUsed; taxable = ebt - nolUsed; }
      const tax = taxable * inp.taxRate;
      const netIncome = ebt - tax;
      const capex = revenue * (at(inp.capexPct, i) ?? 0);
      const dNwc = (revenue - prevRev) * (at(inp.nwcPct, i) ?? 0);
      const fcf = netIncome + da - capex - dNwc; // levered FCF before debt repayment
      const cashConversion = ebitda > 0 ? (ebitda - capex - dNwc) / ebitda : null; // n.m. when EBITDA <= 0

      cash = openCash + fcf;
      // Mandatory amortisation (contractual, in tranche order)
      const mandatory = tranches.map((t, k) => Math.min(openBal[k], t.amortPct * t.amount));
      mandatory.forEach((m, k) => { bal[k] -= m; });
      cash -= sum(mandatory);
      // Liquidity: revolver then equity cure
      let revolverDraw = 0, equityCure = 0;
      if (cash < inp.minCash) {
        const capacity = Math.max(0, (inp.revolver.commitment || 0) - revolver);
        revolverDraw = Math.min(inp.minCash - cash, capacity);
        revolver += revolverDraw; cash += revolverDraw;
      }
      if (cash < 0) { equityCure = -cash; cash = 0; }
      // Excess cash: repay revolver, then sweep
      let revolverRepay = 0;
      const sweep = tranches.map(() => 0);
      let excess = Math.max(0, cash - inp.minCash);
      if (excess > 0 && revolver > 0) {
        revolverRepay = Math.min(revolver, excess);
        revolver -= revolverRepay; cash -= revolverRepay; excess -= revolverRepay;
      }
      let sweepPool = excess * inp.cashSweepPct;
      tranches.forEach((t, k) => {
        if (!t.sweep || sweepPool <= 0) return;
        const pay = Math.min(bal[k], sweepPool);
        sweep[k] = pay; bal[k] -= pay; sweepPool -= pay; cash -= pay;
      });
      const totalDebt = sum(bal) + revolver;
      const netDebt = totalDebt - cash;

      // Reconciliation checks
      tranches.forEach((t, k) => {
        const diff = openBal[k] - mandatory[k] - sweep[k] - bal[k];
        checks.push({ year: y, check: t.name + ' balance roll-forward', ok: Math.abs(diff) < EPS && bal[k] > -EPS, diff });
      });
      const cashRoll = openCash + fcf - sum(mandatory) - sum(sweep) - revolverRepay + revolverDraw + equityCure - cash;
      checks.push({ year: y, check: 'Cash roll-forward', ok: Math.abs(cashRoll) < EPS, diff: cashRoll });
      checks.push({ year: y, check: 'Cash balance not negative', ok: cash > -EPS, diff: Math.min(0, cash) });
      const revRoll = openRevolver + revolverDraw - revolverRepay - revolver;
      checks.push({ year: y, check: 'Revolver roll-forward and within commitment', ok: Math.abs(revRoll) < EPS && revolver <= (inp.revolver.commitment || 0) + EPS, diff: revRoll });

      if (revolverDraw > 0) warnings.push('Year ' + y + ': revolver drawn ' + revolverDraw.toFixed(1) + ' to maintain minimum cash');
      if (equityCure > 0) warnings.push('Year ' + y + ': cash shortfall of ' + equityCure.toFixed(1) + ' funded by a sponsor equity cure');
      if (cash < inp.minCash - EPS && equityCure === 0) warnings.push('Year ' + y + ': cash below the minimum balance');

      years.push({
        year: y, revenue, growth: g, ebitda, margin, da, ebit,
        trancheInterest, revolverInterest, interestExpense, interestIncome, ebt, nolUsed, tax, netIncome,
        capex, dNwc, fcf, cashConversion,
        openBal, mandatory, sweep, closeBal: bal.slice(),
        openRevolver, revolverDraw, revolverRepay, closeRevolver: revolver,
        openCash, equityCure, closeCash: cash,
        totalDebt, netDebt,
        leverage: ebitda > 0 ? totalDebt / ebitda : null, netLeverage: ebitda > 0 ? netDebt / ebitda : null, // n.m. when EBITDA <= 0
        interestCoverage: safeDiv(ebitda, interestExpense),
        debtRepaid: sum(mandatory) + sum(sweep) + revolverRepay - revolverDraw,
      });
      prevRev = revenue;
    }

    // Exit
    const last = years[N - 1];
    const exitEbitda = last.ebitda;
    const exitEV = exitEbitda * inp.exitMultiple;
    const exitFees = exitEV * inp.exitFeePct;
    const exitDebt = last.totalDebt;
    const exitCash = last.closeCash;
    const exitNetDebt = exitDebt - exitCash;
    const exitEquityRaw = exitEV - exitFees - exitNetDebt;
    const exitEquity = Math.max(0, exitEquityRaw);
    if (exitEquityRaw < 0) warnings.push('Exit enterprise value is below net debt; equity is wiped out');
    const sponsorProceeds = exitEquity * sponsorShare;
    const managementProceeds = exitEquity - sponsorProceeds;
    const cures = years.map((y) => y.equityCure);
    const sponsorCashFlows = [-sponsorEquity, ...years.map((y, i) => -cures[i] + (i === N - 1 ? sponsorProceeds : 0))];
    const sponsorInvested = sponsorEquity + sum(cures);
    const moic = sponsorProceeds / sponsorInvested;
    const irrRes = irr(sponsorCashFlows);

    // Value-creation bridge (all equity holders)
    const entryNetDebt = newDebt - inp.minCash;
    const bridge = {
      ebitdaGrowth: (exitEbitda - inp.entryEbitda) * entryMultipleEff,
      multipleExpansion: (inp.exitMultiple - entryMultipleEff) * exitEbitda,
      debtPaydown: entryNetDebt - exitNetDebt,
      fees: -(transactionFees + financingFees + exitFees),
      equityCures: -sum(cures),
    };
    bridge.total = bridge.ebitdaGrowth + bridge.multipleExpansion + bridge.debtPaydown + bridge.fees + bridge.equityCures;
    const valueCreated = exitEquityRaw - totalEquityIn - sum(cures);

    checks.unshift({ year: 0, check: 'Sources equal uses', ok: Math.abs(totalSources - totalUses) < EPS, diff: totalSources - totalUses });
    checks.push({ year: N, check: 'Exit equity = EV − exit fees − net debt', ok: Math.abs(exitEquityRaw - (exitEV - exitFees - exitDebt + exitCash)) < EPS, diff: 0 });
    checks.push({ year: N, check: 'Sponsor + management proceeds = exit equity', ok: Math.abs(sponsorProceeds + managementProceeds - exitEquity) < EPS, diff: sponsorProceeds + managementProceeds - exitEquity });
    checks.push({ year: N, check: 'Value-creation bridge reconciles', ok: exitEquityRaw < 0 || Math.abs(bridge.total - valueCreated) < 1e-6 * Math.max(1, Math.abs(valueCreated)), diff: bridge.total - valueCreated });

    return {
      ok: true, errors: [], warnings,
      entry: { entryEV, entryMultiple: entryMultipleEff, entryEbitda: inp.entryEbitda, entryRevenue: inp.entryRevenue, newDebt,
        leverage: newDebt / inp.entryEbitda, transactionFees, financingFees, equityPurchasePrice, existingNetDebt,
        sponsorEquity, rollover: inp.rollover, totalEquityIn, sponsorShare, debtShare: newDebt / totalUses, equityShare: totalEquityIn / totalUses },
      sources, uses, totalSources, totalUses, tranches,
      years,
      exit: { exitEbitda, exitMultiple: inp.exitMultiple, exitEV, exitFees, exitDebt, exitCash, exitNetDebt, exitEquity, exitEquityRaw, sponsorProceeds, managementProceeds },
      returns: { moic, irr: irrRes.value, irrNote: irrRes.note, sponsorCashFlows, sponsorInvested, cures: sum(cures) },
      bridge, valueCreated, checks, allChecksPass: checks.every((c) => c.ok),
      conventions: [
        'Annual periods; sponsor equity invested at t = 0 and proceeds received at the end of year ' + N + '.',
        'Cash-free, debt-free purchase: uses equal entry EV plus fees plus cash funded to the balance sheet.',
        'Interest charged on opening balances; no circular average-balance interest.',
        'Tax on positive pre-tax profit after loss carry-forwards; losses carried forward indefinitely.',
        'Mandatory amortisation first, then revolver repayment, then cash sweep in tranche order.',
        'Shortfalls below minimum cash draw the revolver; any remaining deficit is funded by a sponsor equity cure.',
        'IRR solved by bisection on annual sponsor cash flows; MOIC = sponsor proceeds ÷ total sponsor equity invested.',
      ],
    };
  }

  /* Required exit multiple / exit EBITDA for a target sponsor IRR, holding the
   * operating plan and debt path fixed. Closed form: proceeds P solve
   * Σ C_t (1+r)^(N−t) = P, with C_t the sponsor's contributions. */
  function breakEven(lboResult, targetIrr) {
    if (!lboResult || !lboResult.ok) return null;
    const N = lboResult.years.length;
    const cfs = lboResult.returns.sponsorCashFlows;
    // contributions are the negative parts of each year's flow, excluding final proceeds
    let fv = 0;
    for (let t = 0; t <= N; t++) {
      const contrib = t === N ? -(cfs[t] - lboResult.exit.sponsorProceeds) : -cfs[t];
      fv += contrib * Math.pow(1 + targetIrr, N - t);
    }
    const requiredSponsorProceeds = fv;
    const requiredEquity = requiredSponsorProceeds / lboResult.entry.sponsorShare;
    const exitFeePct = lboResult.exit.exitEV > 0 ? lboResult.exit.exitFees / lboResult.exit.exitEV : 0;
    const requiredEV = (requiredEquity + lboResult.exit.exitNetDebt) / (1 - exitFeePct);
    return {
      targetIrr, requiredSponsorProceeds, requiredEquity, requiredEV,
      requiredExitMultiple: safeDiv(requiredEV, lboResult.exit.exitEbitda),
      requiredExitEbitda: safeDiv(requiredEV, lboResult.exit.exitMultiple),
      currentExitMultiple: lboResult.exit.exitMultiple, currentExitEbitda: lboResult.exit.exitEbitda,
    };
  }

  /* Build LBO inputs for a scenario. Scenario fields are absolute values:
   * revenueGrowth (p.a.), exitMargin (EBITDA margin reached linearly by exit),
   * entryMultiple, exitMultiple, leverage (total x EBITDA, split pro rata to
   * the base tranches), rateShift (added to all floating rates), holdYears,
   * capexPct and nwcPct (cash conversion drivers). */
  function applyScenario(base, sc) {
    // Scenarios shift the base operating plan rather than replace it, so the base case reproduces the LBO exactly.
    // Growth: every year moves by (scenario average growth − base average growth).
    // Margin: the gap between scenario and base exit margins is phased in evenly over the hold.
    const hold = sc.holdYears ?? base.holdYears;
    const bh = base.holdYears;
    const bg = Array.from({ length: hold }, (_, i) => at(base.revenueGrowth, i) ?? 0);
    const bm = Array.from({ length: hold }, (_, i) => at(base.ebitdaMargin, i) ?? 0);
    const avgG = sum(Array.from({ length: bh }, (_, i) => at(base.revenueGrowth, i) ?? 0)) / bh;
    const baseExitM = at(base.ebitdaMargin, bh - 1) ?? 0;
    const dG = isNum(sc.revenueGrowth) ? sc.revenueGrowth - avgG : 0;
    const dM = isNum(sc.exitMargin) ? sc.exitMargin - baseExitM : 0;
    const baseLev = sum(base.tranches.map((t) => t.leverage));
    const scale = isNum(sc.leverage) && baseLev > 0 ? sc.leverage / baseLev : 1;
    const multChanged = isNum(sc.entryMultiple) && Math.abs(sc.entryMultiple - (base.entryEVOverride > 0 ? base.entryEVOverride / base.entryEbitda : base.entryMultiple)) > 1e-9;
    return {
      ...base,
      holdYears: hold,
      revenueGrowth: bg.map((g) => g + dG),
      ebitdaMargin: bm.map((m, i) => m + dM * (i + 1) / hold),
      entryMultiple: sc.entryMultiple ?? base.entryMultiple,
      entryEVOverride: multChanged ? null : base.entryEVOverride,
      exitMultiple: sc.exitMultiple ?? base.exitMultiple,
      tranches: base.tranches.map((t) => ({ ...t, leverage: t.leverage * scale })),
      rateShift: (base.rateShift || 0) + (sc.rateShift || 0),
      capexPct: sc.capexPct ?? base.capexPct,
      nwcPct: sc.nwcPct ?? base.nwcPct,
    };
  }

  /* Chart series derived from an LBO result, kept here so charts and tests share one definition. */
  function chartSeries(r) {
    if (!r || !r.ok) return null;
    const feePct = r.exit.exitEV ? r.exit.exitFees / r.exit.exitEV : 0;
    // Sponsor equity value if the business were sold at the end of each year at the exit multiple
    const equityByExitYear = r.years.map((y) => Math.max(0, y.ebitda * r.exit.exitMultiple * (1 - feePct) - y.netDebt) * r.entry.sponsorShare);
    const bridge = [
      { label: 'Equity invested', value: r.entry.totalEquityIn, total: true },
      { label: 'EBITDA growth', value: r.bridge.ebitdaGrowth },
      { label: 'Multiple change', value: r.bridge.multipleExpansion },
      { label: 'Debt paydown', value: r.bridge.debtPaydown - r.returns.cures },
      { label: 'Fees & costs', value: r.bridge.fees },
      ...(r.returns.cures ? [{ label: 'Equity cures', value: r.returns.cures }] : []),
      { label: 'Exit equity', value: r.exit.exitEquityRaw, total: true },
    ];
    const debtByTranche = r.tranches.map((t, k) => ({ name: t.name, values: [t.amount, ...r.years.map((y) => y.closeBal[k])] }));
    const revolver = [0, ...r.years.map((y) => y.closeRevolver)];
    return { equityByExitYear, bridge, debtByTranche, revolver, leverage: [r.entry.leverage, ...r.years.map((y) => y.leverage)], interestCover: r.years.map((y) => y.interestCoverage) };
  }

  function maxEntryMultiple(lboIn, targetIrr) {
    const irrAt = (m) => { const r = runLBO({ ...lboIn, entryMultiple: m, entryEVOverride: null }); return r.ok && isNum(r.returns.irr) ? r.returns.irr : null; };
    const start = isNum(lboIn.entryEVOverride) && lboIn.entryEVOverride > 0 && lboIn.entryEbitda > 0 ? lboIn.entryEVOverride / lboIn.entryEbitda : lboIn.entryMultiple;
    if (!isNum(start) || start <= 0) return null;
    let lo = start, hi = start; const v0 = irrAt(start);
    if (!isNum(v0)) return null;
    if (v0 > targetIrr) { for (let i = 0; i < 60; i++) { hi *= 1.15; const v = irrAt(hi); if (!isNum(v) || v <= targetIrr) break; lo = hi; if (hi > 200) return null; } }
    else {
      for (let i = 0; i < 60; i++) {
        const prev = lo; lo /= 1.15; const v = irrAt(lo);
        if (!isNum(v)) {
          // Below some price the deal cannot be modelled (e.g. EV below existing net debt). Find that floor between
          // lo (infeasible) and prev (feasible); the hurdle can only be met if it is met at the floor.
          let bad = lo, good = prev;
          for (let k = 0; k < 80; k++) { const mid = (bad + good) / 2; if (isNum(irrAt(mid))) good = mid; else bad = mid; }
          lo = good; hi = prev; break;
        }
        if (v > targetIrr) break; hi = lo;
      }
    }
    if (!(irrAt(lo) > targetIrr)) return null;
    for (let i = 0; i < 80; i++) { const mid = (lo + hi) / 2, v = irrAt(mid); if (isNum(v) && v > targetIrr) lo = mid; else hi = mid; }
    return lo;
  }

  function sensitivity(rowVals, colVals, fn) {
    return rowVals.map((r) => colVals.map((c) => {
      try { return fn(r, c); } catch (e) { return null; }
    }));
  }

  // ---------- screener ----------
  const DEFAULT_SCORING = {
    weights: { quality: 25, growth: 20, valuation: 20, cash: 15, resilience: 10, fit: 10 },
    ranges: {
      margin: [0.05, 0.30],      // EBITDA margin: 5% → 0 pts, 30% → 100 pts
      growth: [-0.05, 0.20],     // revenue growth
      multiple: [14, 5],         // EV/EBITDA: 14x → 0, 5x → 100 (lower is better)
      conversion: [0.2, 0.9],    // (EBITDA − capex) / EBITDA
      leverage: [5, 0],          // net debt / EBITDA: 5x → 0, ≤0x → 100
    },
    targetSectors: [], targetGeographies: [],
  };
  const lerpScore = (v, [a, b]) => clamp(((v - a) / (b - a)) * 100, 0, 100);
  function scoreTarget(d, cfg = DEFAULT_SCORING) {
    const W = cfg.weights, R = cfg.ranges;
    const margin = safeDiv(d.ebitda, d.revenue);
    const mult = d.ev != null && d.ebitda > 0 ? safeDiv(d.ev, d.ebitda) : null;
    const lev = d.netDebt != null && d.ebitda > 0 ? safeDiv(d.netDebt, d.ebitda) : null;
    const conv = d.capex != null && d.ebitda > 0 ? safeDiv(d.ebitda - d.capex, d.ebitda) : null;
    const pct = (x) => (x * 100).toFixed(1) + '%';
    const comps = [];
    comps.push(margin === null ? { key: 'quality', label: 'Business quality & profitability', score: null, why: 'Revenue or EBITDA missing' }
      : { key: 'quality', label: 'Business quality & profitability', score: lerpScore(margin, R.margin), value: margin, why: 'EBITDA margin ' + pct(margin) + ' against a ' + pct(R.margin[0]) + '–' + pct(R.margin[1]) + ' scale' });
    comps.push(!isNum(d.revenueGrowth) ? { key: 'growth', label: 'Growth', score: null, why: 'Revenue growth missing' }
      : { key: 'growth', label: 'Growth', score: lerpScore(d.revenueGrowth, R.growth), value: d.revenueGrowth, why: 'Revenue growth ' + pct(d.revenueGrowth) + ' against a ' + pct(R.growth[0]) + '–' + pct(R.growth[1]) + ' scale' });
    comps.push(mult === null ? { key: 'valuation', label: 'Valuation attractiveness', score: null, why: d.ebitda != null && d.ebitda <= 0 ? 'EBITDA not positive; multiple not meaningful' : 'Enterprise value or EBITDA missing' }
      : { key: 'valuation', label: 'Valuation attractiveness', score: lerpScore(mult, R.multiple), value: mult, why: mult.toFixed(1) + 'x EV/EBITDA; ' + R.multiple[1] + 'x scores 100, ' + R.multiple[0] + 'x scores 0' });
    comps.push(conv === null ? { key: 'cash', label: 'Cash generation', score: null, why: 'Capex not provided' }
      : { key: 'cash', label: 'Cash generation', score: lerpScore(conv, R.conversion), value: conv, why: 'Cash conversion (EBITDA − capex) / EBITDA of ' + pct(conv) });
    comps.push(lev === null ? { key: 'resilience', label: 'Financial resilience', score: null, why: 'Net debt or EBITDA missing' }
      : { key: 'resilience', label: 'Financial resilience', score: lerpScore(lev, R.leverage), value: lev, why: lev <= 0 ? 'Net cash position' : 'Net leverage ' + lev.toFixed(1) + 'x EBITDA' });
    let fit = null, fitWhy;
    if (isNum(d.strategicFit)) { fit = clamp((d.strategicFit - 1) / 4 * 100, 0, 100); fitWhy = 'Analyst fit rating ' + d.strategicFit + ' of 5'; }
    else if ((cfg.targetSectors || []).length || (cfg.targetGeographies || []).length) {
      const s = (cfg.targetSectors || []).length ? (cfg.targetSectors.includes(d.sector) ? 50 : 0) : 25;
      const g = (cfg.targetGeographies || []).length ? (cfg.targetGeographies.includes(d.geography) ? 50 : 0) : 25;
      fit = s + g; fitWhy = 'Sector ' + (s === 50 ? 'in' : s === 0 ? 'outside' : 'not set for') + ' focus list; geography ' + (g === 50 ? 'in' : g === 0 ? 'outside' : 'not set for') + ' focus list';
    } else fitWhy = 'No fit rating and no focus sectors or geographies set';
    comps.push({ key: 'fit', label: 'Strategic acquisition fit', score: fit, why: fitWhy });
    let wSum = 0, sSum = 0, wAll = 0;
    comps.forEach((c) => { c.weight = W[c.key] || 0; wAll += c.weight; if (c.score !== null) { wSum += c.weight; sSum += c.weight * c.score; } });
    comps.forEach((c) => { c.contribution = c.score !== null && wSum > 0 ? (c.weight * c.score) / wSum : null; });
    const total = wSum > 0 ? sSum / wSum : null;
    return { total, components: comps, coverage: wAll > 0 ? wSum / wAll : 0 };
  }

  function passesFilters(d, f) {
    const reasons = [];
    const chk = (label, v, test) => {
      if (v === null || v === undefined || !isNum(v)) { reasons.push(label + ': no data'); return; }
      if (!test(v)) reasons.push(label);
    };
    const margin = safeDiv(d.ebitda, d.revenue);
    const mult = d.ebitda > 0 ? safeDiv(d.ev, d.ebitda) : null;
    const lev = d.ebitda > 0 ? safeDiv(d.netDebt, d.ebitda) : null;
    if (isNum(f.minRevenue)) chk('Revenue below minimum', d.revenue, (v) => v >= f.minRevenue);
    if (isNum(f.maxRevenue)) chk('Revenue above maximum', d.revenue, (v) => v <= f.maxRevenue);
    if (isNum(f.minEbitda)) chk('EBITDA below minimum', d.ebitda, (v) => v >= f.minEbitda);
    if (isNum(f.minMargin)) chk('Margin below minimum', margin, (v) => v >= f.minMargin);
    if (isNum(f.minGrowth)) chk('Growth below minimum', d.revenueGrowth, (v) => v >= f.minGrowth);
    if (isNum(f.maxMultiple)) chk('Entry multiple above maximum', mult, (v) => v <= f.maxMultiple);
    if (isNum(f.maxLeverage)) chk('Net leverage above maximum', lev, (v) => v <= f.maxLeverage);
    if (f.sectors && f.sectors.length && !f.sectors.includes(d.sector)) reasons.push('Sector not selected');
    if (f.geographies && f.geographies.length && !f.geographies.includes(d.geography)) reasons.push('Geography not selected');
    if (f.verifiedOnly && d.dataStatus !== 'verified') reasons.push('Not verified data');
    if (f.minPeriod && (!d.reportingPeriod || String(d.reportingPeriod) < f.minPeriod)) reasons.push('Reporting period older than ' + f.minPeriod);
    return { pass: reasons.length === 0, reasons };
  }

  // ---------- IC recommendation ----------
  const DEFAULT_CRITERIA = { requireVerified: true, minBaseIrr: 0.20, minBaseMoic: 2.0, minDownsideMoic: 1.0, maxEntryLeverage: 6.0, minScore: 55, passBelowIrr: 0.12, passBelowDownsideMoic: 0.7 };
  function recommend(m, c = DEFAULT_CRITERIA) {
    const checks = [];
    const add = (label, ok, detail) => checks.push({ label, ok, detail });
    const p = (x) => (isNum(x) ? (x * 100).toFixed(1) + '%' : 'n/a');
    const x = (v) => (isNum(v) ? v.toFixed(2) + 'x' : 'n/a');
    add('Base-case IRR at or above hurdle', isNum(m.baseIrr) && m.baseIrr >= c.minBaseIrr, p(m.baseIrr) + ' vs ' + p(c.minBaseIrr) + ' hurdle');
    add('Base-case MOIC at or above target', isNum(m.baseMoic) && m.baseMoic >= c.minBaseMoic, x(m.baseMoic) + ' vs ' + x(c.minBaseMoic));
    add('Downside MOIC protects capital', isNum(m.downsideMoic) && m.downsideMoic >= c.minDownsideMoic, x(m.downsideMoic) + ' vs ' + x(c.minDownsideMoic));
    add('Entry leverage within limit', isNum(m.entryLeverage) && m.entryLeverage <= c.maxEntryLeverage, x(m.entryLeverage) + ' vs ' + x(c.maxEntryLeverage) + ' maximum');
    add('Attractiveness score at or above minimum', isNum(m.score) && m.score >= c.minScore, (isNum(m.score) ? m.score.toFixed(0) : 'n/a') + ' vs ' + c.minScore);
    add('All model checks reconcile', !!m.checksPass, m.checksPass ? 'Sources/uses, debt and cash reconcile' : 'One or more reconciliation checks failed');
    if (c.requireVerified) add('Key figures checked against their sources', !!m.dataVerified, m.dataVerified ? 'Data label: verified' : 'Data label: ' + (m.dataLabel || 'unverified'));
    let decision, rationale;
    const passTrig = [];
    if (isNum(m.baseIrr) && m.baseIrr < c.passBelowIrr) passTrig.push('base IRR ' + p(m.baseIrr) + ' is below the ' + p(c.passBelowIrr) + ' pass threshold');
    if (isNum(m.downsideMoic) && m.downsideMoic < c.passBelowDownsideMoic) passTrig.push('downside MOIC ' + x(m.downsideMoic) + ' is below ' + x(c.passBelowDownsideMoic));
    if (passTrig.length) { decision = 'Pass'; rationale = 'Pass because ' + passTrig.join(' and ') + '.'; }
    else if (checks.every((k) => k.ok)) { decision = 'Proceed'; rationale = 'All ' + checks.length + ' criteria are met.'; }
    else { const f = checks.filter((k) => !k.ok).map((k) => k.label.toLowerCase()); decision = 'Further Research'; rationale = 'Further work needed: ' + f.join('; ') + '.'; }
    return { decision, rationale, checks };
  }

  // ---------- CSV ----------
  function parseCSV(text) {
    const rows = []; let row = [], field = '', q = false;
    const s = String(text || '').replace(/^﻿/, '');
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (q) {
        if (ch === '"') { if (s[i + 1] === '"') { field += '"'; i++; } else q = false; }
        else field += ch;
      } else if (ch === '"') q = true;
      else if (ch === ',') { row.push(field); field = ''; }
      else if (ch === '\n' || ch === '\r') {
        if (ch === '\r' && s[i + 1] === '\n') i++;
        row.push(field); field = '';
        if (row.some((c) => c.trim() !== '')) rows.push(row);
        row = [];
      } else field += ch;
    }
    row.push(field);
    if (row.some((c) => c.trim() !== '')) rows.push(row);
    return rows;
  }
  /* Parse a number from a CSV cell. '' / 'n/a' → null (unknown); '0' → 0.
   * Accepts 1,234.5  (12.3)  £5m  12%  (percent cells return decimals). */
  function parseNumber(cell, { percent = false } = {}) {
    if (cell === null || cell === undefined) return null;
    let t = String(cell).trim();
    if (t === '' || /^(n\/?a|na|null|-|—|nm|n\.m\.)$/i.test(t)) return null;
    let neg = false;
    if (/^\(.*\)$/.test(t)) { neg = true; t = t.slice(1, -1); }
    const hasPct = t.includes('%');
    t = t.replace(/[£$€,\s%]/g, '').replace(/m$/i, '');
    if (t.startsWith('-')) { neg = !neg; t = t.slice(1); }
    if (!/^\d*\.?\d+(e[+-]?\d+)?$/i.test(t)) return NaN;
    let v = parseFloat(t);
    if (neg) v = -v;
    if (percent && (hasPct || Math.abs(v) > 1.5)) v = v / 100;
    return v;
  }
  const CSV_COLUMNS = [
    { key: 'name', label: 'company_name', required: true },
    { key: 'ticker', label: 'ticker' },
    { key: 'sector', label: 'sector' },
    { key: 'industry', label: 'industry' },
    { key: 'headquarters', label: 'headquarters' },
    { key: 'geography', label: 'geography' },
    { key: 'currency', label: 'currency' },
    { key: 'revenue', label: 'revenue_m', number: true },
    { key: 'ebitda', label: 'ebitda_m', number: true },
    { key: 'ev', label: 'enterprise_value_m', number: true },
    { key: 'revenueGrowth', label: 'revenue_growth_pct', number: true, percent: true },
    { key: 'netDebt', label: 'net_debt_m', number: true },
    { key: 'capex', label: 'capex_m', number: true },
    { key: 'reportingPeriod', label: 'reporting_period' },
    { key: 'source', label: 'source' },
    { key: 'dataStatus', label: 'data_status' },
  ];
  function importCSV(text) {
    const rows = parseCSV(text);
    if (!rows.length) return { records: [], errors: ['The file is empty'] };
    const header = rows[0].map((h) => h.trim().toLowerCase());
    const idx = {};
    CSV_COLUMNS.forEach((c) => { idx[c.key] = header.indexOf(c.label); });
    const errors = [];
    if (idx.name < 0) return { records: [], errors: ['Missing required column "company_name". Download the template for the expected headers.'] };
    const records = [];
    rows.slice(1).forEach((r, ri) => {
      const rowErr = [];
      const rec = {};
      CSV_COLUMNS.forEach((c) => {
        const cell = idx[c.key] >= 0 ? r[idx[c.key]] : undefined;
        if (c.number) {
          const v = parseNumber(cell, { percent: c.percent });
          if (Number.isNaN(v)) { rowErr.push(c.label + ' "' + cell + '" is not a number'); rec[c.key] = null; }
          else rec[c.key] = v;
        } else rec[c.key] = cell !== undefined ? String(cell).trim() : '';
      });
      if (!rec.name) rowErr.push('company_name is empty');
      if (isNum(rec.ebitda) && isNum(rec.revenue) && rec.ebitda > rec.revenue) rowErr.push('EBITDA exceeds revenue');
      if (isNum(rec.revenue) && rec.revenue < 0) rowErr.push('Revenue is negative');
      const st = (rec.dataStatus || '').toLowerCase();
      // A file cannot vouch for itself: a claimed 'verified' or 'sourced' imports as 'sourced' (claimed source, unchecked)
      rec.dataStatus = st === 'verified' || st === 'sourced' ? 'sourced' : st === 'demo' ? 'demo' : 'unverified';
      if (rowErr.length) errors.push('Row ' + (ri + 2) + ': ' + rowErr.join('; '));
      else records.push(rec);
    });
    return { records, errors };
  }
  function toCSV(rows) {
    const esc = (v) => {
      if (v === null || v === undefined) return '';
      const s = String(v);
      return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    return rows.map((r) => r.map(esc).join(',')).join('\r\n');
  }

  // ---------- formatting ----------
  const SYM = { GBP: '£', USD: '$', EUR: '€' };
  const fmt = {
    money(v, cur = 'GBP', dp = 1) {
      if (!isNum(v)) return '—';
      const s = (SYM[cur] || cur + ' ') + Math.abs(v).toLocaleString('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp }) + 'm';
      return v < 0 ? '(' + s + ')' : s;
    },
    num(v, dp = 1) {
      if (!isNum(v)) return '—';
      const s = Math.abs(v).toLocaleString('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp });
      return v < 0 ? '(' + s + ')' : s;
    },
    // A value that rounds to zero is shown without a sign: never (0.0%) or -0.0x.
    pct(v, dp = 1) { if (!isNum(v)) return '—'; const s = Math.abs(v * 100).toFixed(dp) + '%'; return v < 0 && +s.slice(0, -1) !== 0 ? '(' + s + ')' : s; },
    mult(v, dp = 1) { if (!isNum(v)) return '—'; const s = v.toFixed(dp); return (+s === 0 ? (0).toFixed(dp) : s) + 'x'; },
    date(iso) { if (!iso) return '—'; const d = new Date(iso); return isNaN(d) ? String(iso) : d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }); },
  };

  const KCI = { isNum, safeDiv, sum, npv, irr, quantile, stats, evBridge, MULTIPLES, compMultiples, compsAnalysis, wacc, runDCF,
    runLBO, validateLBO, breakEven, applyScenario, sensitivity, maxEntryMultiple, chartSeries, DEFAULT_SCORING, scoreTarget, passesFilters,
    DEFAULT_CRITERIA, recommend, parseCSV, parseNumber, CSV_COLUMNS, importCSV, toCSV, fmt };
  if (typeof module !== 'undefined' && module.exports) module.exports = KCI;
  else root.KCI = KCI;
})(typeof globalThis !== 'undefined' ? globalThis : this);
