"""Generate NB06 as a Jupyter notebook JSON."""
import json, pathlib

NB_PATH = pathlib.Path(__file__).parent / "source" / "06_exposure_characterisation_geoai.ipynb"

def md(source):
    return {"cell_type": "markdown", "metadata": {}, "source": source if isinstance(source, list) else [source]}

def code(source):
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": source if isinstance(source, list) else [source]}

cells = []

cells.append(md([
    "# Notebook 06 — Environmental Epidemiology & GeoAI Synthesis\n",
    "**GeoAI for Climate, Environment and Health** | 25 June 2026\n",
    "\n---\n\n",
    "## Learning objectives\n",
    "- Map adverse birth outcomes across Zimbabwe provinces and identify spatial patterns\n",
    "- Quantify cluster-level correlations between environmental exposures and birth outcomes\n",
    "- Fit a logistic regression: P(LBW) ~ heat + drought + vegetation + socioeconomic factors\n",
    "- Read and interpret odds ratios and 95% confidence intervals in a forest plot\n",
    "- Understand why individual-level and ecological-level associations can differ\n",
    "- Use a GeoAI natural-language interface to query the epidemiological database\n",
    "\n---\n\n",
    "> **Context.** By this point you have loaded real Zimbabwe DHS 2015 birth records,\n",
    "> explored satellite-derived climate data, performed spatial joins, and built trimester-level\n",
    "> exposure summaries. This notebook is the synthesis: we ask whether environmental conditions\n",
    "> during pregnancy are associated with adverse birth outcomes, and what we can — and cannot — infer.\n",
]))

cells.append(code([
    "import pandas as pd\n",
    "import numpy as np\n",
    "import matplotlib.pyplot as plt\n",
    "import matplotlib.patches as mpatches\n",
    "import duckdb, sys, pathlib, warnings\n",
    "warnings.filterwarnings('ignore')\n",
    "from scipy import stats\n",
    "import statsmodels.api as sm\n",
    "\n",
    "WORKSHOP_DIR = pathlib.Path().resolve().parent\n",
    "DATA_DIR = WORKSHOP_DIR / 'data'\n",
    "sys.path.insert(0, str(WORKSHOP_DIR))\n",
    "\n",
    "births   = pd.read_csv(DATA_DIR / 'zw_births.csv')\n",
    "clusters = pd.read_csv(DATA_DIR / 'zw_clusters.csv')\n",
    "climate  = pd.read_csv(DATA_DIR / 'zw_climate_monthly.csv')\n",
    "\n",
    "clim_ann = climate.groupby('cluster_id').agg(\n",
    "    mean_lst  = ('lst_day_c', 'mean'),\n",
    "    mean_rain = ('chirps_mm', 'mean'),\n",
    "    mean_ndvi = ('ndvi',      'mean'),\n",
    ").reset_index()\n",
    "\n",
    "b     = births.merge(clim_ann, on='cluster_id', how='left')\n",
    "b_lbw = b[b['low_birth_weight'].notna()].copy()\n",
    "\n",
    "print(f'Total births 2011-2015:     {len(births):,}')\n",
    "print(f'With recorded birth weight: {len(b_lbw):,} ({100*len(b_lbw)/len(births):.0f}%)')\n",
    "print(f'Low birth weight cases:     {int(b_lbw[\"low_birth_weight\"].sum())} ({100*b_lbw[\"low_birth_weight\"].mean():.1f}%)')\n",
    "print(f'Neonatal deaths:            {births[\"neonatal_death\"].sum()} ({1000*births[\"neonatal_death\"].mean():.1f}/1,000 births)')\n",
]))

# Part A
cells.append(md([
    "---\n## Part A — Where are adverse outcomes concentrated?\n\n",
    "Before modelling, we examine the spatial distribution of outcomes. Zimbabwe spans dramatic\n",
    "gradients: the cool Eastern Highlands (Manicaland), hot dry lowveld (Masvingo, Matabeleland South),\n",
    "and urban centres (Harare, Bulawayo).\n",
]))

cells.append(md(["### ✅ Worked example A1 — Province-level outcome table\n"]))
cells.append(code([
    "prov = b.groupby('province').agg(\n",
    "    n_births         = ('neonatal_death', 'count'),\n",
    "    lbw_n            = ('low_birth_weight', 'sum'),\n",
    "    lbw_denom        = ('low_birth_weight', lambda x: x.notna().sum()),\n",
    "    neonatal_deaths  = ('neonatal_death', 'sum'),\n",
    "    mean_lst         = ('mean_lst', 'mean'),\n",
    "    mean_ndvi        = ('mean_ndvi', 'mean'),\n",
    "    pct_urban        = ('urban', 'mean'),\n",
    ").reset_index()\n",
    "\n",
    "prov['lbw_pct']         = (prov['lbw_n'] / prov['lbw_denom'] * 100).round(1)\n",
    "prov['neonatal_per1000'] = (prov['neonatal_deaths'] / prov['n_births'] * 1000).round(1)\n",
    "\n",
    "display_cols = prov[['province','lbw_pct','neonatal_per1000','mean_lst','mean_ndvi','pct_urban']].copy()\n",
    "display_cols['mean_lst']  = display_cols['mean_lst'].round(1)\n",
    "display_cols['mean_ndvi'] = display_cols['mean_ndvi'].round(3)\n",
    "display_cols['pct_urban'] = (display_cols['pct_urban']*100).round(0)\n",
    "display_cols.columns = ['Province','LBW %','Neonatal MR/1000','LST (°C)','NDVI','% Urban']\n",
    "print(display_cols.sort_values('LBW %', ascending=False).to_string(index=False))\n",
]))

cells.append(md(["### ✅ Worked example A2 — Cluster scatter maps: LBW and LST\n"]))
cells.append(code([
    "cl_merged = clusters.merge(clim_ann, on='cluster_id')\n",
    "cl_lbw    = cl_merged[cl_merged['lbw_denom'] >= 5].copy()\n",
    "\n",
    "fig, axes = plt.subplots(1, 2, figsize=(14, 6))\n",
    "\n",
    "sc1 = axes[0].scatter(cl_lbw['cluster_lon'], cl_lbw['cluster_lat'],\n",
    "    c=cl_lbw['lbw_rate']*100, cmap='YlOrRd', s=30, alpha=0.7, vmin=0, vmax=25)\n",
    "plt.colorbar(sc1, ax=axes[0], label='LBW rate (%)')\n",
    "axes[0].set_title('Cluster LBW Rate', fontsize=12, fontweight='bold')\n",
    "axes[0].set_xlabel('Longitude'); axes[0].set_ylabel('Latitude')\n",
    "\n",
    "sc2 = axes[1].scatter(cl_merged['cluster_lon'], cl_merged['cluster_lat'],\n",
    "    c=cl_merged['mean_ndvi'], cmap='YlGn', s=30, alpha=0.7, vmin=0.15, vmax=0.55)\n",
    "plt.colorbar(sc2, ax=axes[1], label='Mean NDVI')\n",
    "axes[1].set_title('Mean NDVI 2011–2015', fontsize=12, fontweight='bold')\n",
    "axes[1].set_xlabel('Longitude'); axes[1].set_ylabel('Latitude')\n",
    "\n",
    "plt.suptitle('Zimbabwe DHS 2015: Birth Outcomes and Environmental Conditions',\n",
    "             fontsize=12, fontweight='bold')\n",
    "plt.tight_layout()\n",
    "plt.show()\n",
    "print('Visual: Do high-LBW clusters (dark red) tend to occur in lower-NDVI (yellow) areas?')\n",
]))

# Exercise 1
cells.append(md([
    "---\n### 🧪 Exercise 1 — Cluster-level ecological correlations *(3 points)*\n\n",
    "Using only clusters with `lbw_denom >= 5`, compute the Pearson correlation between:\n",
    "1. Mean annual NDVI and cluster LBW rate → store as `r_ndvi_lbw` (float, 3 dp)\n",
    "2. Mean annual LST and cluster LBW rate → store as `r_lst_lbw` (float, 3 dp)\n\n",
    "Plot both as scatter plots with regression lines.\n",
]))
cells.append(code([
    "### BEGIN SOLUTION\n",
    "cl_eco = cl_merged[cl_merged['lbw_denom'] >= 5].copy()\n",
    "\n",
    "r_ndvi_lbw_raw, p_ndvi = stats.pearsonr(cl_eco['mean_ndvi'], cl_eco['lbw_rate'])\n",
    "r_lst_lbw_raw,  p_lst  = stats.pearsonr(cl_eco['mean_lst'],  cl_eco['lbw_rate'])\n",
    "r_ndvi_lbw = round(r_ndvi_lbw_raw, 3)\n",
    "r_lst_lbw  = round(r_lst_lbw_raw,  3)\n",
    "\n",
    "fig, axes = plt.subplots(1, 2, figsize=(12, 5))\n",
    "for ax, xcol, r_val, p_val, xlabel in [\n",
    "    (axes[0], 'mean_ndvi', r_ndvi_lbw, p_ndvi, 'Mean annual NDVI'),\n",
    "    (axes[1], 'mean_lst',  r_lst_lbw,  p_lst,  'Mean annual LST (°C)'),\n",
    "]:\n",
    "    x = cl_eco[xcol]; y = cl_eco['lbw_rate'] * 100\n",
    "    ax.scatter(x, y, alpha=0.4, s=15, color='#2a9d8f')\n",
    "    m, b_ = np.polyfit(x, y, 1)\n",
    "    xline = np.linspace(x.min(), x.max(), 100)\n",
    "    ax.plot(xline, m*xline+b_, 'r-', lw=2)\n",
    "    ax.set_xlabel(xlabel); ax.set_ylabel('Cluster LBW rate (%)')\n",
    "    ax.set_title(f'r = {r_val:.3f}  (p = {p_val:.3f})', fontsize=11)\n",
    "\n",
    "plt.suptitle('Cluster-level Environmental Correlates of Low Birth Weight', fontsize=12, fontweight='bold')\n",
    "plt.tight_layout(); plt.show()\n",
    "\n",
    "print(f'r(NDVI, LBW rate) = {r_ndvi_lbw}  (p={p_ndvi:.3f})')\n",
    "print(f'r(LST,  LBW rate) = {r_lst_lbw}   (p={p_lst:.3f})')\n",
    "### END SOLUTION\n",
]))
cells.append(code([
    "### BEGIN HIDDEN TESTS\n",
    "assert isinstance(r_ndvi_lbw, float), 'r_ndvi_lbw must be a float'\n",
    "assert isinstance(r_lst_lbw,  float), 'r_lst_lbw must be a float'\n",
    "assert r_ndvi_lbw < 0, 'Expected negative: greener clusters → lower LBW'\n",
    "assert r_lst_lbw  > 0, 'Expected positive: hotter clusters → higher LBW'\n",
    "print('✅ PASS')\n",
    "### END HIDDEN TESTS\n",
]))

# Part B
cells.append(md([
    "---\n## Part B — Individual-level logistic regression\n\n",
    "We now move from ecological (cluster-level) to individual-level analysis.\n",
    "**Outcome**: low birth weight (< 2,500 g). **Predictors**: trimester-specific\n",
    "environmental exposures + socioeconomic and maternal factors.\n\n",
    "> An **odds ratio (OR)** of 1.0 = no association. OR > 1 = higher odds of LBW.\n",
    "> OR < 1 = lower odds. The 95% CI tells us the range of plausible values.\n",
    "> If the CI crosses 1.0, the association is not statistically significant at α = 0.05.\n",
]))

cells.append(md(["### ✅ Worked example B1 — Fit the logistic regression\n"]))
cells.append(code([
    "mod_df = b_lbw.copy()\n",
    "mod_df['edu_num'] = mod_df['education'].map({'none':0,'primary':1,'secondary':2,'higher':3})\n",
    "\n",
    "# Standardise continuous predictors for comparable coefficients\n",
    "for col in ['lst_t3', 'rain_t3', 'ndvi_t3', 'mother_age']:\n",
    "    mu, sd = mod_df[col].mean(), mod_df[col].std()\n",
    "    mod_df[f'{col}_z'] = (mod_df[col] - mu) / sd\n",
    "\n",
    "X = sm.add_constant(mod_df[[\n",
    "    'lst_t3_z', 'rain_t3_z', 'ndvi_t3_z',\n",
    "    'urban', 'wealth_quintile', 'birth_order', 'edu_num', 'mother_age_z'\n",
    "]].astype(float))\n",
    "y = mod_df['low_birth_weight'].astype(float)\n",
    "\n",
    "model = sm.Logit(y, X).fit(disp=False)\n",
    "\n",
    "odds   = np.exp(model.params[1:])\n",
    "ci_lo  = np.exp(model.conf_int().iloc[1:, 0])\n",
    "ci_hi  = np.exp(model.conf_int().iloc[1:, 1])\n",
    "pvals  = model.pvalues[1:]\n",
    "\n",
    "labels = [\n",
    "    'T3 temperature (+1 SD)', 'T3 rainfall (+1 SD)', 'T3 NDVI (+1 SD)',\n",
    "    'Urban cluster', 'Wealth quintile (+1)',\n",
    "    'Birth order (+1)', 'Education (+1 level)', 'Maternal age (+1 SD)',\n",
    "]\n",
    "\n",
    "results = pd.DataFrame({'OR': odds.values, 'CI_lo': ci_lo.values, 'CI_hi': ci_hi.values,\n",
    "                         'p': pvals.values}, index=labels)\n",
    "results['Sig'] = results['p'].apply(\n",
    "    lambda p: '***' if p<0.001 else ('**' if p<0.01 else ('*' if p<0.05 else '')))\n",
    "results['OR']    = results['OR'].round(3)\n",
    "results['CI_lo'] = results['CI_lo'].round(3)\n",
    "results['CI_hi'] = results['CI_hi'].round(3)\n",
    "results['p']     = results['p'].round(3)\n",
    "\n",
    "print(results.to_string())\n",
    "print(f\"\\nPseudo R-squared (McFadden): {model.prsquared:.3f}\")\n",
]))

cells.append(md(["### ✅ Worked example B2 — Forest plot\n"]))
cells.append(code([
    "fig, ax = plt.subplots(figsize=(9, 6))\n",
    "y_pos = np.arange(len(results))\n",
    "\n",
    "colours = ['#2a9d8f' if ch < 1 else ('#e63946' if cl > 1 else '#888888')\n",
    "           for cl, ch in zip(results['CI_lo'], results['CI_hi'])]\n",
    "\n",
    "ax.errorbar(\n",
    "    results['OR'], y_pos,\n",
    "    xerr=[results['OR']-results['CI_lo'], results['CI_hi']-results['OR']],\n",
    "    fmt='o', ecolor='k', capsize=4, capthick=1.5, markersize=8,\n",
    "    markerfacecolor=colours, markeredgecolor='k', lw=0,\n",
    ")\n",
    "ax.axvline(1.0, color='black', linestyle='--', lw=1.2)\n",
    "ax.set_yticks(y_pos)\n",
    "ax.set_yticklabels(results.index, fontsize=10)\n",
    "ax.set_xlabel('Odds Ratio (95% CI)', fontsize=11)\n",
    "ax.set_title('Predictors of Low Birth Weight — Zimbabwe DHS 2015\\nLogistic regression, n = {:,}'.format(len(y)),\n",
    "             fontsize=11, fontweight='bold')\n",
    "ax.set_xlim(0.45, 1.85)\n",
    "\n",
    "handles = [\n",
    "    mpatches.Patch(color='#2a9d8f', label='OR < 1 (protective — CI excludes 1.0)'),\n",
    "    mpatches.Patch(color='#e63946', label='OR > 1 (risk — CI excludes 1.0)'),\n",
    "    mpatches.Patch(color='#888888', label='CI crosses 1.0 (inconclusive)'),\n",
    "]\n",
    "ax.legend(handles=handles, loc='lower right', fontsize=9)\n",
    "ax.grid(axis='x', alpha=0.3)\n",
    "plt.tight_layout()\n",
    "plt.show()\n",
]))

cells.append(md([
    "> **Findings.** Significant predictors: maternal education (protective), birth order (protective),\n",
    "> maternal age (slight risk). Environmental predictors (heat, rainfall, NDVI) are in the expected\n",
    "> directions but confidence intervals cross 1.0.\n\n",
    "> **Why?** Three reasons: (1) cluster jitter means exposure is imprecise;\n",
    "> (2) birth weight is only recorded for ~30% of births — selection is non-random\n",
    "> (facility-delivered, urban births overrepresented); (3) monthly CMC birth date gives\n",
    "> only a rough proxy for trimester exposure timing, introducing noise.\n",
    "> **Environmental epidemiology: study design and data quality determine what we detect.**\n",
]))

# Exercise 2
cells.append(md([
    "---\n### 🧪 Exercise 2 — Interpret and extend the model *(4 points)*\n\n",
    "**(a)** Extract the environmental predictor rows from `results` into `env_results` (DataFrame).\n\n",
    "**(b)** Compute the **province-level** Pearson correlation between mean NDVI and LBW rate.\n",
    "Store as `r_prov_ndvi_lbw` (float, 3 dp). Compare to the individual-level r.\n",
    "Store `'stronger'` or `'weaker'` in `ndvi_level_comparison`.\n\n",
    "**(c)** Write 2–3 sentences in the free-text cell below explaining why the ecological\n",
    "association between NDVI and LBW is stronger than the individual-level association.\n",
]))
cells.append(code([
    "### BEGIN SOLUTION\n",
    "env_labels = ['T3 temperature (+1 SD)', 'T3 rainfall (+1 SD)', 'T3 NDVI (+1 SD)']\n",
    "env_results = results.loc[env_labels].copy()\n",
    "\n",
    "prov_agg = b.groupby('province').agg(\n",
    "    lbw_rate  = ('low_birth_weight', 'mean'),\n",
    "    mean_ndvi = ('mean_ndvi', 'mean'),\n",
    ").dropna()\n",
    "r_raw, _ = stats.pearsonr(prov_agg['mean_ndvi'], prov_agg['lbw_rate'])\n",
    "r_prov_ndvi_lbw = round(r_raw, 3)\n",
    "\n",
    "r_ind, _ = stats.pearsonr(b_lbw['ndvi_t3'].dropna(), b_lbw['low_birth_weight'].dropna())\n",
    "ndvi_level_comparison = 'stronger' if abs(r_prov_ndvi_lbw) > abs(r_ind) else 'weaker'\n",
    "\n",
    "print('Environmental ORs:')\n",
    "print(env_results)\n",
    "print(f'\\nProvince NDVI ~ LBW: r = {r_prov_ndvi_lbw}')\n",
    "print(f'Individual NDVI_T3 ~ LBW: r = {r_ind:.3f}')\n",
    "print(f'Ecological association is {ndvi_level_comparison} than individual-level')\n",
    "### END SOLUTION\n",
]))
cells.append(code([
    "### BEGIN HIDDEN TESTS\n",
    "assert isinstance(env_results, pd.DataFrame) and len(env_results) == 3\n",
    "assert isinstance(r_prov_ndvi_lbw, float) and r_prov_ndvi_lbw < 0\n",
    "assert ndvi_level_comparison in ('stronger', 'weaker')\n",
    "print('✅ PASS')\n",
    "### END HIDDEN TESTS\n",
]))
cells.append(md(["*(Write 2–3 sentences: why is the ecological NDVI–LBW association stronger than the individual-level one?)*\n"]))

# Part C
cells.append(md(["---\n## Part C — Seasonality and exposure categories\n"]))

cells.append(md(["### ✅ Worked example C1 — LBW rates by environmental category\n"]))
cells.append(code([
    "cl_cat = cl_merged[['cluster_id','mean_lst','mean_rain','mean_ndvi']].copy()\n",
    "cl_cat['hot_cluster']   = cl_cat['mean_lst']  > cl_cat['mean_lst'].median()\n",
    "cl_cat['green_cluster'] = cl_cat['mean_ndvi'] > cl_cat['mean_ndvi'].median()\n",
    "b3 = b.merge(cl_cat, on='cluster_id')\n",
    "\n",
    "fig, axes = plt.subplots(1, 2, figsize=(10, 5))\n",
    "for ax, col, cats, colours, title in [\n",
    "    (axes[0], 'hot_cluster',   [False, True],  ['#457b9d','#e63946'], 'Heat category'),\n",
    "    (axes[1], 'green_cluster', [False, True],  ['#e9c46a','#2a9d8f'], 'Vegetation category'),\n",
    "]:\n",
    "    rates = b3[b3['low_birth_weight'].notna()].groupby(col)['low_birth_weight'].mean() * 100\n",
    "    xlabels = ['Cool','Hot'] if col=='hot_cluster' else ['Sparse','Green']\n",
    "    bars = ax.bar(xlabels, [rates.get(c, 0) for c in cats], color=colours, width=0.5)\n",
    "    for bar, val in zip(bars, [rates.get(c,0) for c in cats]):\n",
    "        ax.text(bar.get_x()+bar.get_width()/2, val+0.2, f'{val:.1f}%', ha='center', va='bottom', fontsize=12)\n",
    "    ax.set_ylabel('LBW rate (%)'); ax.set_xlabel(title); ax.set_ylim(0, 13)\n",
    "\n",
    "plt.suptitle('LBW Rate by Cluster Environmental Category', fontsize=12, fontweight='bold')\n",
    "plt.tight_layout(); plt.show()\n",
]))

# Exercise 3
cells.append(md([
    "---\n### 🧪 Exercise 3 — Seasonality of LBW *(3 points)*\n\n",
    "Compute the LBW rate (%) by **birth month** (1=Jan, …, 12=Dec).\n",
    "Store as `monthly_lbw` (Series, index=birth_month). Plot with a twin-axis showing mean monthly LST.\n",
    "Store:\n",
    "- `peak_lbw_month` — int: the birth month with the highest LBW rate\n",
    "- `peak_lbw_season` — string: 'rainy' or 'dry' (rainy season = months 10, 11, 12, 1, 2, 3, 4)\n",
]))
cells.append(code([
    "### BEGIN SOLUTION\n",
    "monthly_lbw  = (b_lbw.groupby('birth_month')['low_birth_weight'].mean() * 100).round(2)\n",
    "peak_lbw_month  = int(monthly_lbw.idxmax())\n",
    "rainy_months = {10,11,12,1,2,3,4}\n",
    "peak_lbw_season = 'rainy' if peak_lbw_month in rainy_months else 'dry'\n",
    "\n",
    "monthly_lst = climate.groupby('month')['lst_day_c'].mean()\n",
    "\n",
    "fig, ax1 = plt.subplots(figsize=(10, 5))\n",
    "ax2 = ax1.twinx()\n",
    "months = list(range(1,13))\n",
    "ax1.bar(months, [monthly_lbw.get(m, np.nan) for m in months], color='#e63946', alpha=0.7, label='LBW rate (%)')\n",
    "ax2.plot(months, [monthly_lst.get(m, np.nan) for m in months], 'b-o', lw=2, ms=6, label='Mean LST (°C)')\n",
    "for m in rainy_months:\n",
    "    ax1.axvspan(m-0.5, m+0.5, alpha=0.1, color='#457b9d')\n",
    "ax1.set_xticks(months)\n",
    "ax1.set_xticklabels(['J','F','M','A','M','J','J','A','S','O','N','D'])\n",
    "ax1.set_xlabel('Birth month'); ax1.set_ylabel('LBW rate (%)', color='#e63946')\n",
    "ax2.set_ylabel('Mean LST (°C)', color='blue')\n",
    "ax1.set_title('Monthly LBW rate and mean LST (blue shading = rainy season)', fontweight='bold')\n",
    "l1,lb1 = ax1.get_legend_handles_labels()\n",
    "l2,lb2 = ax2.get_legend_handles_labels()\n",
    "ax1.legend(l1+l2, lb1+lb2, loc='upper left')\n",
    "plt.tight_layout(); plt.show()\n",
    "\n",
    "print(f'Peak LBW birth month: {peak_lbw_month} ({peak_lbw_season} season)')\n",
    "### END SOLUTION\n",
]))
cells.append(code([
    "### BEGIN HIDDEN TESTS\n",
    "assert isinstance(monthly_lbw, pd.Series) and len(monthly_lbw) == 12\n",
    "assert isinstance(peak_lbw_month, int) and 1 <= peak_lbw_month <= 12\n",
    "assert peak_lbw_season in ('rainy','dry')\n",
    "print('✅ PASS')\n",
    "### END HIDDEN TESTS\n",
]))

# Part D: DuckDB + GeoAI
cells.append(md([
    "---\n## Part D — DuckDB & GeoAI: Querying the epidemiological database\n\n",
    "The workshop database `exposures_zw.duckdb` contains all tables. We can use SQL directly,\n",
    "or use the **GeoAI** natural-language interface to generate queries from plain English.\n",
]))

cells.append(md(["### ✅ Worked example D1 — SQL queries on the database\n"]))
cells.append(code([
    "DB_PATH = str(DATA_DIR / 'exposures_zw.duckdb')\n",
    "con = duckdb.connect(DB_PATH, read_only=True)\n",
    "print('Tables:', con.execute('SHOW TABLES').fetchdf()['name'].tolist())\n",
    "print()\n",
    "\n",
    "# Province LBW rates with environmental context\n",
    "q1 = con.execute(\"\"\"\n",
    "    SELECT province,\n",
    "           COUNT(*)                   AS n_births,\n",
    "           ROUND(100.0 * SUM(low_birth_weight)\n",
    "                 / NULLIF(SUM(CASE WHEN low_birth_weight IS NOT NULL THEN 1 END),0), 1) AS lbw_pct,\n",
    "           ROUND(AVG(mean_lst),  1)   AS avg_lst_c,\n",
    "           ROUND(AVG(mean_ndvi), 3)   AS avg_ndvi\n",
    "    FROM births\n",
    "    GROUP BY province ORDER BY lbw_pct DESC\n",
    "\"\"\").fetchdf()\n",
    "print(q1.to_string(index=False))\n",
]))

cells.append(code([
    "# Drought-exposed vs non-drought birth outcomes\n",
    "q2 = con.execute(\"\"\"\n",
    "    SELECT drought_exposed,\n",
    "           COUNT(*)  AS n,\n",
    "           ROUND(100.0 * SUM(low_birth_weight)\n",
    "                 / NULLIF(SUM(CASE WHEN low_birth_weight IS NOT NULL THEN 1 END),0), 1) AS lbw_pct,\n",
    "           ROUND(AVG(birth_weight_g), 0) AS mean_bw_g,\n",
    "           ROUND(1000.0 * SUM(neonatal_death) / COUNT(*), 1) AS neonatal_mr\n",
    "    FROM births GROUP BY drought_exposed ORDER BY drought_exposed\n",
    "\"\"\").fetchdf()\n",
    "print(q2)\n",
]))

# Exercise 4
cells.append(md([
    "---\n### 🧪 Exercise 4 — Write your own SQL query *(2 points)*\n\n",
    "Write a SQL query returning the **top 3 provinces by neonatal mortality rate** (per 1,000 births),\n",
    "with columns for n_births, neonatal_deaths, and the rate. Store as `top_nm_provinces`.\n",
]))
cells.append(code([
    "### BEGIN SOLUTION\n",
    "top_nm_provinces = con.execute(\"\"\"\n",
    "    SELECT province,\n",
    "           COUNT(*) AS n_births,\n",
    "           SUM(neonatal_death) AS neonatal_deaths,\n",
    "           ROUND(1000.0 * SUM(neonatal_death) / COUNT(*), 1) AS neonatal_mr_per1000\n",
    "    FROM births\n",
    "    GROUP BY province\n",
    "    ORDER BY neonatal_mr_per1000 DESC\n",
    "    LIMIT 3\n",
    "\"\"\").fetchdf()\n",
    "print(top_nm_provinces)\n",
    "### END SOLUTION\n",
]))
cells.append(code([
    "### BEGIN HIDDEN TESTS\n",
    "assert isinstance(top_nm_provinces, pd.DataFrame) and len(top_nm_provinces) == 3\n",
    "assert 'province' in top_nm_provinces.columns\n",
    "assert any('rate' in c.lower() or 'mr' in c.lower() for c in top_nm_provinces.columns)\n",
    "print('✅ PASS')\n",
    "### END HIDDEN TESTS\n",
]))

# GeoAI section
cells.append(md([
    "---\n## 📖 GeoAI — Natural language to SQL\n\n",
    "The GeoAI interface below sends your question to a large language model (Claude Haiku)\n",
    "running on the PRECISE server. The model is given the database schema and returns:\n",
    "1. The SQL query it generated\n",
    "2. The query results\n\n",
    "**Try questions like:**\n",
    "- *\"Which 5 clusters have the highest LBW rate among rural clusters with at least 10 weighed births?\"*\n",
    "- *\"Compare mean T3 temperature for LBW vs normal-weight births\"*\n",
    "- *\"Show neonatal mortality rate by education level and wealth quintile\"*\n",
    "- *\"What fraction of births occurred during the rainy season in each province?\"*\n",
]))
cells.append(code([
    "import urllib.request as _ureq, json as _json, os as _os\n",
    "\n",
    "GEOAI_URL = _os.environ.get('GEOAI_API_URL', 'http://localhost:8000/geoai/query')\n",
    "\n",
    "def geoai(question: str) -> None:\n",
    "    \"\"\"Send a plain-English question to the GeoAI endpoint.\"\"\"\n",
    "    payload = _json.dumps({'question': question, 'db': 'exposures_zw'}).encode()\n",
    "    try:\n",
    "        req = _ureq.Request(GEOAI_URL, data=payload,\n",
    "                            headers={'Content-Type': 'application/json'}, method='POST')\n",
    "        with _ureq.urlopen(req, timeout=30) as resp:\n",
    "            res = _json.loads(resp.read())\n",
    "        print('SQL generated:')\n",
    "        print(res.get('sql', '(none)'))\n",
    "        df = pd.DataFrame(res.get('rows', []), columns=res.get('columns', []))\n",
    "        print('\\nResults:')\n",
    "        print(df.to_string(index=False) if len(df) > 0 else '(no rows)')\n",
    "    except Exception as e:\n",
    "        print(f'GeoAI unavailable ({e}). In a connected session this returns AI-generated SQL and results.')\n",
    "\n",
    "geoai('Which 5 clusters have the highest LBW rate among rural clusters with at least 10 weighed births?')\n",
]))
cells.append(code([
    "# Try your own question here\n",
    "geoai('Compare mean T3 temperature and T3 NDVI for LBW versus normal-weight births')\n",
]))

# Exercise 5: free text synthesis
cells.append(md([
    "---\n### 🧪 Exercise 5 — Synthesis *(4 points)*\n\n",
    "Write a 4–6 sentence epidemiological summary of what this analysis found. Address:\n",
    "1. The spatial distribution of LBW across Zimbabwe and which provinces are highest\n",
    "2. Which environmental factors show the clearest association with LBW and at which level of analysis\n",
    "3. Why environmental effects were weaker in the individual-level logistic regression\n",
    "4. Two key study limitations and how they could be addressed in future work\n\n",
    "Your facilitator will mark this on: correct interpretation of ecological vs individual-level results,\n",
    "understanding of confounding/selection bias/measurement error, and quality of suggested improvements.\n",
]))
cells.append(md(["*(Write your 4–6 sentence synthesis here)*\n"]))

# Recap
cells.append(md([
    "---\n## 🏁 Workshop recap — What we built today\n\n",
    "| Step | What | Tools |\n",
    "|------|------|-------|\n",
    "| NB01 | Loaded and mapped real Zimbabwe DHS 2015 birth records | pandas, matplotlib |\n",
    "| NB02 | Explored CHIRPS/MODIS satellite climate data; saw El Niño 2015 signal | Python, matplotlib |\n",
    "| NB03 | Joined health data to environmental grid; quantified cluster jitter uncertainty | scipy.cKDTree |\n",
    "| NB04 | Built trimester exposure windows; linked pregnancy timing to seasons | pandas |\n",
    "| NB05 | Understood Earth Engine products, aggregation, cloud-fill | GEE concepts |\n",
    "| NB06 | Environmental epidemiology: ecological + individual-level analysis, ORs, GeoAI | statsmodels, DuckDB |\n\n",
    "**The central message:** Satellite-derived environmental data can be linked to population health\n",
    "records at scale. The associations found here — greener provinces have lower LBW; hotter\n",
    "clusters have higher LBW — are consistent with biological pathways (heat stress, nutritional\n",
    "stress via vegetation). But study design and data quality determine what we can detect.\n",
    "Environmental epidemiology requires careful attention to confounding, selection bias, and\n",
    "exposure measurement error. These are research design problems that cannot be solved by\n",
    "better satellite data alone.\n\n",
    "---\n",
    "*Data: Zimbabwe Demographic and Health Survey 2015 (DHS Program). Climate: simulated\n",
    "exposure grids calibrated to Zimbabwe seasonal patterns and El Niño 2015 conditions.*\n",
]))

notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.10.0"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

NB_PATH.write_text(json.dumps(notebook, indent=1))
print(f"Written: {NB_PATH}")
print(f"Cells: {len(cells)}")
