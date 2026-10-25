"""Labelled query set for the Phase 3 data research.

Each in-corpus query: (text, scheme_id, expected chunk group). Out-of-corpus: (text, scheme_id|None).
Golden factual questions are loaded from tests/eval/golden.yaml; the rest are hand-written
paraphrases that avoid the section labels on purpose ("hard" set).
"""

LC, MC, SC, MU, SV = (
    "hdfc-large-cap", "hdfc-mid-cap", "hdfc-small-cap", "hdfc-multi-cap", "hdfc-silver-etf-fof",
)

# fact key -> chunk group
FACT_GROUP = {
    "category": "category_risk", "risk_level": "category_risk", "nav": "nav",
    "fund_size": "fund_size", "expense_ratio": "expense_ratio", "min_sip": "minimum_investments",
    "min_lumpsum": "minimum_investments", "min_additional": "minimum_investments",
    "exit_load": "exit_load", "stamp_duty": "stamp_duty", "tax_implication": "tax_implication",
    "fund_managers": "fund_management", "investment_objective": "investment_objective",
    "benchmark": "benchmark", "holdings": "holdings", "fund_house": "fund_house",
    "custodian": "custodian", "registrar": "registrar",
}

PARAPHRASES = [
    # expense ratio
    ("How much does HDFC Mid Cap Fund charge every year to manage my money?", MC, "expense_ratio"),
    ("What's the TER on HDFC Large Cap direct?", LC, "expense_ratio"),
    ("annual fees for hdfc silver fof", SV, "expense_ratio"),
    ("What is the total expense ratio of the multi cap fund?", MU, "expense_ratio"),
    # exit load
    ("Is there a penalty if I withdraw from HDFC Small Cap Fund early?", SC, "exit_load"),
    ("Will I be charged if I sell my HDFC Silver ETF FoF units after a week?", SV, "exit_load"),
    ("redemption charges hdfc multi cap", MU, "exit_load"),
    # minimum investments
    ("What's the smallest amount I can start a monthly SIP with in HDFC Large Cap Fund?", LC, "minimum_investments"),
    ("How much money do I need to invest in HDFC Multi Cap Fund the first time?", MU, "minimum_investments"),
    ("Can I top up HDFC Small Cap Fund with a small amount later? what's the minimum?", SC, "minimum_investments"),
    ("min investment hdfc midcap", MC, "minimum_investments"),
    # category / risk
    ("How risky is HDFC Small Cap Fund?", SC, "category_risk"),
    ("What type of fund is HDFC Silver ETF FoF?", SV, "category_risk"),
    ("riskometer hdfc large cap", LC, "category_risk"),
    ("Which category does HDFC Multi Cap Fund belong to?", MU, "category_risk"),
    # nav
    ("What is the current price of one unit of HDFC Large Cap Fund?", LC, "nav"),
    ("Today's NAV of hdfc silver fof?", SV, "nav"),
    # fund size
    ("How big is HDFC Small Cap Fund?", SC, "fund_size"),
    ("What is the AUM of HDFC Large Cap Fund?", LC, "fund_size"),
    ("How much money is managed in HDFC Silver ETF FoF?", SV, "fund_size"),
    # stamp duty
    ("Is there any stamp duty when I buy HDFC Mid Cap Fund?", MC, "stamp_duty"),
    ("What government charge applies on purchase of HDFC Multi Cap Fund units?", MU, "stamp_duty"),
    # tax
    ("What is the STCG tax on HDFC Large Cap Fund?", LC, "tax_implication"),
    ("How much tax do I pay on long term gains from HDFC Mid Cap Fund?", MC, "tax_implication"),
    ("Is HDFC Silver ETF FoF taxed like an equity fund?", SV, "tax_implication"),
    # fund managers
    ("Who runs HDFC Small Cap Fund?", SC, "fund_management"),
    ("Since when has Chirag Setalvad managed HDFC Mid Cap Fund?", MC, "fund_management"),
    ("What is the educational background of the fund manager of HDFC Large Cap Fund?", LC, "fund_management"),
    ("Who is the portfolio manager for the silver fof?", SV, "fund_management"),
    # objective
    ("What is the aim of HDFC Multi Cap Fund?", MU, "investment_objective"),
    ("What is HDFC Large Cap Fund trying to achieve?", LC, "investment_objective"),
    ("Where does HDFC Silver ETF FoF put its money?", SV, "investment_objective"),
    # benchmark
    ("Which index does HDFC Small Cap Fund track?", SC, "benchmark"),
    ("What is HDFC Silver ETF FoF compared against?", SV, "benchmark"),
    ("hdfc large cap bench mark", LC, "benchmark"),
    # holdings
    ("What are the top holdings of HDFC Mid Cap Fund?", MC, "holdings"),
    ("Which stocks does HDFC Large Cap Fund own?", LC, "holdings"),
    ("Does HDFC Multi Cap Fund hold Zomato?", MU, "holdings"),
    ("How many stocks are in the HDFC Small Cap portfolio?", SC, "holdings"),
    ("What percentage of HDFC Silver ETF FoF is in the Silver ETF?", SV, "holdings"),
    ("How much of HDFC Large Cap Fund is invested in ICICI Bank?", LC, "holdings"),
    # fund house
    ("What is the phone number of the AMC for HDFC Mid Cap Fund?", MC, "fund_house"),
    ("Which company manages HDFC Small Cap Fund?", SC, "fund_house"),
    ("What is the address of HDFC Mutual Fund?", MU, "fund_house"),
    # registrar / custodian
    ("Who is the RTA for HDFC Multi Cap Fund?", MU, "registrar"),
    ("Who handles the transfer agency for HDFC Silver ETF FoF?", SV, "registrar"),
    ("Which bank is the custodian of HDFC Large Cap Fund?", LC, "custodian"),
    ("Who keeps the securities of HDFC Small Cap Fund in safe custody?", SC, "custodian"),
]

# Holdings questions that need a *specific* part (entity in a later part), keyed by expected chunk id.
HOLDINGS_DEEP = [
    ("Does HDFC Small Cap Fund hold Goodyear India?", SC, "groww-hdfc-small-cap:holdings:3"),
    ("What weight does HDFC Small Cap Fund give Sula Vineyards?", SC, "groww-hdfc-small-cap:holdings:3"),
    ("Does HDFC Multi Cap Fund own Timken India?", MU, "groww-hdfc-multi-cap:holdings:1"),
]

# Out of corpus, but a scheme is named, so retrieval runs *filtered* to that scheme and will always
# return something: only the evidence threshold (or the LLM) can stop an answer.
OOC_SCHEME = [
    ("Does HDFC Mid Cap Fund have a lock-in period?", MC),
    ("When was HDFC Mid Cap Fund launched?", MC),
    ("What is the Groww rating of HDFC Multi Cap Fund?", MU),
    ("How can I get my account statement for HDFC Mid Cap Fund?", MC),
    ("What is the minimum redemption amount for HDFC Large Cap Fund?", LC),
    ("What is the portfolio turnover ratio of HDFC Small Cap Fund?", SC),
    ("What is the Sharpe ratio of HDFC Multi Cap Fund?", MU),
    ("What is the standard deviation of HDFC Mid Cap Fund?", MC),
    ("Does HDFC Large Cap Fund have an IDCW option?", LC),
    ("How do I redeem my units of HDFC Small Cap Fund?", SC),
    ("Which SIP dates are available for HDFC Large Cap Fund?", LC),
    ("Can I set up an SWP in HDFC Silver ETF FoF?", SV),
    ("What is the P/E ratio of HDFC Large Cap Fund?", LC),
    ("What is the tracking error of HDFC Silver ETF FoF?", SV),
    ("How do I add a nominee to my HDFC Mid Cap Fund folio?", MC),
    ("Who is the CEO of the AMC of HDFC Small Cap Fund?", SC),
]

# Near-miss: same section matches strongly, but the specific thing asked isn't on the page.
# Retrieval *cannot* tell these apart by score; the LLM's `insufficient` status must.
OOC_NEAR_MISS = [
    ("What is the expense ratio of the regular plan of HDFC Mid Cap Fund?", MC),
    ("What was the expense ratio of HDFC Large Cap Fund last year?", LC),
    ("What was the exit load of HDFC Small Cap Fund in 2019?", SC),
]

# Out of corpus with no supported scheme named -> unfiltered search over all 80 chunks.
OOC_GENERAL = [
    ("What is the lock-in period for ELSS funds?", None),
    ("How do I download my capital gains statement?", None),
    ("How do I report mutual fund capital gains in my income tax return?", None),
    ("How do I complete my KYC?", None),
    ("What is the difference between direct and regular plans?", None),
    ("How do I change the bank mandate for my SIP?", None),
    ("What is a mutual fund?", None),
    ("What is the price of silver today?", None),
    ("What is the current repo rate?", None),
    ("How do I update my nominee?", None),
    # Another AMC's scheme: the resolver finds no supported scheme and no deny-listed one.
    ("What is the exit load of SBI Bluechip Fund?", None),
    ("What is the expense ratio of Axis Midcap Fund?", None),
    ("Who manages Parag Parikh Flexi Cap Fund?", None),
]

# Held-out set: written before any chunk/BM25 variant was tried; never used to pick synonyms.
HELDOUT = [
    ("What do I pay yearly to stay invested in HDFC Small Cap Fund?", SC, "expense_ratio"),
    ("expense ratio for hdfc large cap?", LC, "expense_ratio"),
    ("Silver FoF ongoing cost percentage", SV, "expense_ratio"),
    ("If I exit HDFC Large Cap Fund in 6 months do I lose anything?", LC, "exit_load"),
    ("Any charges for leaving the mid cap fund before a year?", MC, "exit_load"),
    ("Can I start HDFC Silver ETF FoF with Rs 100?", SV, "minimum_investments"),
    ("Lowest SIP allowed in the multi cap scheme?", MU, "minimum_investments"),
    ("Is HDFC Mid Cap Fund high risk or low risk?", MC, "category_risk"),
    ("Is HDFC Large Cap an equity fund or a debt fund?", LC, "category_risk"),
    ("What's the unit value of the small cap fund right now?", SC, "nav"),
    ("net asset value hdfc mid cap", MC, "nav"),
    ("Total assets of HDFC Multi Cap Fund in crores?", MU, "fund_size"),
    ("How large is the mid cap fund's corpus?", MC, "fund_size"),
    ("Do I pay stamp duty on SIP instalments in HDFC Silver ETF FoF?", SV, "stamp_duty"),
    ("If I sell HDFC Small Cap Fund after two years how is the gain taxed?", SC, "tax_implication"),
    ("capital gains tax rate multi cap fund", MU, "tax_implication"),
    ("Who takes the investment decisions for HDFC Multi Cap Fund?", MU, "fund_management"),
    ("What experience does the HDFC Silver ETF FoF manager have?", SV, "fund_management"),
    ("What is the goal of the HDFC Mid Cap scheme?", MC, "investment_objective"),
    ("What does HDFC Small Cap Fund invest in?", SC, "investment_objective"),
    ("What index is HDFC Mid Cap Fund measured against?", MC, "benchmark"),
    ("benchmark of silver fof", SV, "benchmark"),
    ("Biggest positions in HDFC Small Cap Fund?", SC, "holdings"),
    ("Does the mid cap fund invest in Timken India?", MC, "holdings"),
    ("Is Reliance Industries part of HDFC Multi Cap Fund's portfolio?", MU, "holdings"),
    ("Contact details for the fund house of HDFC Large Cap Fund", LC, "fund_house"),
    ("Who is the AMC behind HDFC Silver ETF FoF?", SV, "fund_house"),
    ("Who is the registrar of HDFC Small Cap Fund?", SC, "registrar"),
    ("Where is CAMS located for HDFC Mid Cap Fund?", MC, "registrar"),
    ("custodian bank hdfc small cap", SC, "custodian"),
]
