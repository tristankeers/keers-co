  const r1 = (v, dp = 4) => isNum(v) ? +v.toFixed(dp) : v;
  function profileRates(d) {
    const rev = isNum(d.revenue) && d.revenue > 0 ? d.revenue : null;
    const da = rev && isNum(d.ebitda) && isNum(d.ebit) && d.ebitda > d.ebit ? Math.min(0.5, (d.ebitda - d.ebit) / rev) : null;
    const cx = rev && isNum(d.capex) && d.capex >= 0 ? Math.min(0.6, d.capex / rev) : null;
    const tax = d.lboDefaults && isNum(d.lboDefaults.taxRate) ? d.lboDefaults.taxRate : null;
    return { daPct: da != null ? da : 0.03, capexPct: cx != null ? cx : 0.03, taxRate: tax != null ? tax : 0.25, daFrom: da !== null, capexFrom: cx !== null, taxFrom: tax !== null };
  }
  function defaultLBO(d) {
    const rev = isNum(d.revenue) && d.revenue > 0 ? d.revenue : null;
    const ebitda = isNum(d.ebitda) && d.ebitda > 0 ? d.ebitda : null;
    const margin2 = rev && ebitda ? ebitda / rev : 0.15;
    const mult = isNum(d.ev) && ebitda ? r1(d.ev / ebitda, 2) : 8;
    const g = isNum(d.revenueGrowth) ? Math.max(-0.05, Math.min(0.2, d.revenueGrowth)) : 0.05;
    const hold = 5;
    const pr = profileRates(d);
    return {
      entryRevenue: rev,
      entryEbitda: ebitda,
      entryMultiple: mult,
      entryEVOverride: null,
      existingDebt: isNum(d.netDebt) && d.netDebt > 0 ? d.netDebt : 0,
      existingCash: isNum(d.netDebt) && d.netDebt < 0 ? -d.netDebt : 0,
      transactionFeePct: 0.025,
      financingFeePct: 0.025,
      minCash: rev ? r1(rev * 0.02, 2) : 0,
      rollover: 0,
      // 4.0x EBITDA (3x senior, 1x subordinated), capped so debt never exceeds 60% of the purchase price.
      tranches: [
        { name: "Senior term loan", leverage: r1(0.75 * Math.min(4, 0.6 * mult), 3), rate: 0.085, amortPct: 0.05, sweep: true },
        { name: "Subordinated notes", leverage: r1(0.25 * Math.min(4, 0.6 * mult), 3), rate: 0.12, amortPct: 0, sweep: false }
      ],
      revolver: { commitment: ebitda ? r1(ebitda * 0.5, 2) : 0, rate: 0.08 },
      cashSweepPct: 0.75,
      interestOnCashRate: 0.02,
      holdYears: hold,
      // Recorded growth fades in a straight line to a long-run rate (3%, or 0% for a shrinking business) by the exit year,
      // rather than compounding a single year's growth for the whole hold.
      revenueGrowth: Array.from({ length: hold }, (_, i) => r1(g + (Math.max(0, Math.min(g, 0.03)) - g) * (hold > 1 ? i / (hold - 1) : 0))),
      // Defaults assume no margin expansion: a starting model should not build in value creation the user has not chosen.
      ebitdaMargin: Array(hold).fill(r1(margin2)),
      daPct: r1(pr.daPct),
      capexPct: r1(pr.capexPct),
      nwcPct: 0.1,
      taxRate: pr.taxRate,
      exitMultiple: mult,
      exitFeePct: 0.01,
      assumptionNote: "Defaults are starting assumptions generated from the target profile: debt of 4.0x EBITDA or 60% of the price if lower, margin held flat, exit at the entry multiple, " + (isNum(d.revenueGrowth) ? "recorded revenue growth (capped at \u22125% to 20%) fading to 3% by exit" : "revenue growth of 5% fading to 3% by exit because none is recorded") + "; D&A " + (pr.daFrom ? "from EBITDA less EBIT" : "at 3% of revenue") + ", capex " + (pr.capexFrom ? "as reported" : "at 3% of revenue") + ", tax " + (pr.taxFrom ? "at " + Math.round(pr.taxRate * 100) + "% (set on the record: " + (d.lboDefaults.taxNote || "see notes") + ")" : "at 25%") + ". Edit every input before relying on outputs."
    };
  }
  function fitYears(lbo) {
    const n = lbo.holdYears;
    const fit = (a, fill) => {
      const arr = Array.isArray(a) ? a.slice(0, n) : [];
      while (arr.length < n) arr.push(arr.length ? arr[arr.length - 1] : fill);
      return arr;
    };
    return { ...lbo, revenueGrowth: fit(lbo.revenueGrowth, 0.05), ebitdaMargin: fit(lbo.ebitdaMargin, 0.15) };
  }
  function defaultScenarios(lbo) {
    const hold = lbo.holdYears;
    const g = lbo.revenueGrowth.reduce((s, v) => s + v, 0) / lbo.revenueGrowth.length;
    const m = lbo.ebitdaMargin[hold - 1];
    const lev = lbo.tranches.reduce((s, t) => s + t.leverage, 0);
    const effMult = isNum(lbo.entryEVOverride) && lbo.entryEVOverride > 0 && lbo.entryEbitda > 0 ? lbo.entryEVOverride / lbo.entryEbitda : lbo.entryMultiple;
    const base = { revenueGrowth: g, exitMargin: m, entryMultiple: effMult, exitMultiple: lbo.exitMultiple, leverage: lev, rateShift: 0, holdYears: hold, capexPct: lbo.capexPct, nwcPct: lbo.nwcPct };
    return {
      downside: { ...base, revenueGrowth: r1(g - 0.04), exitMargin: r1(m - 0.02), exitMultiple: r1(lbo.exitMultiple - 1.5, 2), rateShift: 0.015, capexPct: r1(lbo.capexPct + 5e-3) },
      base,
      upside: { ...base, revenueGrowth: r1(g + 0.03), exitMargin: r1(m + 0.015), exitMultiple: r1(lbo.exitMultiple + 1, 2) }
    };
  }
  function defaultDCF(d) {
    var _a, _b;
    const margin2 = isNum(d.revenue) && isNum(d.ebitda) && d.revenue > 0 ? d.ebitda / d.revenue : 0.15;
    const g = isNum(d.revenueGrowth) ? Math.max(-0.2, Math.min(0.3, d.revenueGrowth)) : 0.05;
    const pr = profileRates(d);
    return {
      baseRevenue: (_a = d.revenue) != null ? _a : null,
      years: 5,
      revenueGrowth: [g, g * 0.9, g * 0.8, g * 0.7, g * 0.6].map((v) => r1(v)),
      ebitdaMargin: Array(5).fill(r1(margin2)),
      daPct: r1(pr.daPct),
      capexPct: r1(pr.capexPct),
      nwcPct: 0.1,
      taxRate: pr.taxRate,
      waccMode: "build",
      wacc: 0.11,
      riskFree: 0.045,
      beta: 1.1,
      erp: 0.055,
      sizePremium: 0.02,
      preTaxCostOfDebt: 0.08,
      debtWeight: 0.3,
      terminalMethod: "both",
      terminalGrowth: 0.02,
      exitMultiple: isNum(d.ev) && d.ebitda > 0 ? r1(d.ev / d.ebitda, 2) : 8,
      midYear: true,
      netDebt: (_b = d.netDebt) != null ? _b : null,
      otherClaims: 0,
      nonOperatingAssets: 0,
      sharesOutstanding: null
    };
  }
  function fitDcf(dcf) {
    const n = dcf.years;
    const fit = (a) => {
      var _a;
      const arr = Array.isArray(a) ? a.slice(0, n) : [a];
      while (arr.length < n) arr.push((_a = arr[arr.length - 1]) != null ? _a : 0);
      return arr;
    };
    return { ...dcf, revenueGrowth: fit(dcf.revenueGrowth), ebitdaMargin: fit(dcf.ebitdaMargin) };
  }
  function dcfWacc(dcf) {
    if (dcf.waccMode === "direct") return { ok: isNum(dcf.wacc), value: dcf.wacc, errors: isNum(dcf.wacc) ? [] : ["WACC is required"] };
    return K.wacc({ riskFree: dcf.riskFree, beta: dcf.beta, erp: dcf.erp, sizePremium: dcf.sizePremium, preTaxCostOfDebt: dcf.preTaxCostOfDebt, taxRate: dcf.taxRate, debtWeight: dcf.debtWeight });
  }
  const SC_KEYS = ["revenueGrowth", "exitMargin", "entryMultiple", "exitMultiple", "leverage", "rateShift", "holdYears", "capexPct", "nwcPct"];
  function scenarioBaseDrift(saved, current) {
    return SC_KEYS.filter((k) => isNum(saved[k]) && isNum(current[k]) ? Math.abs(saved[k] - current[k]) > 1e-9 : saved[k] !== current[k]);
  }
  function rebaseScenarios(saved, lboIn) {
    const nb = defaultScenarios(lboIn).base, ob = saved.base, out = { base: { ...nb } };
    for (const k of ["downside", "upside"]) {
      out[k] = {};
      SC_KEYS.forEach((f) => {
        const off = isNum(saved[k][f]) && isNum(ob[f]) ? saved[k][f] - ob[f] : 0;
        out[k][f] = f === "holdYears" ? nb[f] + Math.round(off) : nb[f] + off;
      });
    }
    return out;
  }
  function analyse(deal, settings) {
    var _a, _b;
    const lboIn = fitYears(deal.lbo || defaultLBO(deal));
    const lbo = K.runLBO(lboIn);
    const scIn = deal.scenarios || defaultScenarios(lboIn);
    const scenarioDrift = deal.scenarios ? scenarioBaseDrift(deal.scenarios.base, defaultScenarios(lboIn).base) : [];
    const scenarios = {};
    for (const k of ["downside", "base", "upside"]) {
      const inp = fitYears(K.applyScenario(lboIn, scIn[k]));
      scenarios[k] = { inputs: scIn[k], model: inp, result: K.runLBO(inp) };
    }
    const score = K.scoreTarget(deal, settings.scoring);
    const dcfIn = fitDcf(deal.dcf || defaultDCF(deal));
    const w = dcfWacc(dcfIn);
    const dcf = w.ok ? K.runDCF({ ...dcfIn, wacc: w.value }) : { ok: false, errors: w.errors, warnings: [] };
    const comps = K.compsAnalysis(deal.comps || [], { target: { revenue: deal.revenue, ebitda: deal.ebitda, ebit: (_a = deal.ebit) != null ? _a : null, netIncome: (_b = deal.netIncome) != null ? _b : null, netDebt: deal.netDebt } });
    const base = scenarios.base.result, down = scenarios.downside.result;
    const rec = K.recommend({
      baseIrr: base.ok ? base.returns.irr : null,
      baseMoic: base.ok ? base.returns.moic : null,
      downsideMoic: down.ok ? down.returns.moic : null,
      entryLeverage: base.ok ? base.entry.leverage : null,
      score: score.total,
      checksPass: base.ok && base.allChecksPass && lbo.ok && lbo.allChecksPass,
      dataVerified: deal.dataStatus === "verified",
      dataLabel: deal.dataStatus === "demo" ? "DEMO DATA" : deal.dataStatus
    }, settings.criteria);
    return { lboIn, lbo, scIn, scenarios, scenarioDrift, score, dcfIn, wacc: w, dcf, comps, rec };
  }
