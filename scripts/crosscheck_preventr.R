# PREVENT base-model ASCVD risk from preventr, for the cross-check.
#
# Usage: Rscript crosscheck_preventr.R INPUT.csv OUTPUT.csv
#
# INPUT columns: id, horizon (10 or 30), sex, age, tc, hdl, sbp, bp_treated,
# statin, diabetes, smoker, egfr, bmi.
# OUTPUT columns: id, horizon, ascvd_rounded, ascvd_full, input_problems.
#
# ascvd_rounded is the public estimate_risk() output, which preventr rounds to
# 3 decimals. ascvd_full calls preventr's internal prep_terms() and
# run_models() with dp = 15, the same code path without the 3-decimal rounding.
# preventr rejects SBP > 180 (both columns are NA for those rows).

suppressPackageStartupMessages(library(preventr))
stopifnot(packageVersion("preventr") == "0.12.2")

args <- commandArgs(trailingOnly = TRUE)
stopifnot(length(args) == 2)
inp <- read.csv(args[1], stringsAsFactors = FALSE)

dat <- data.frame(
  age = inp$age,
  sex = inp$sex,
  sbp = inp$sbp,
  bp_tx = as.logical(inp$bp_treated),
  total_c = inp$tc,
  hdl_c = inp$hdl,
  statin = as.logical(inp$statin),
  dm = as.logical(inp$diabetes),
  smoking = as.logical(inp$smoker),
  egfr = inp$egfr,
  bmi = inp$bmi
)

out <- data.frame(
  id = inp$id,
  horizon = inp$horizon,
  ascvd_rounded = NA_real_,
  ascvd_full = NA_real_,
  input_problems = NA_character_
)

for (h in c(10, 30)) {
  rows <- which(inp$horizon == h)
  time <- paste0(h, "yr")
  res <- estimate_risk(
    use_dat = dat[rows, ], model = "base", time = time,
    quiet = TRUE, progress = FALSE, add_to_dat = FALSE
  )
  res <- as.data.frame(res)
  stopifnot(nrow(res) == length(rows))
  out$ascvd_rounded[rows] <- res$ascvd
  out$input_problems[rows] <- res$input_problems

  for (i in rows) {
    if (is.na(out$ascvd_rounded[i])) next
    terms <- preventr:::prep_terms(dat[i, ], "base", "mg/dL")
    full <- preventr:::run_models("base", dat$sex[i], time, terms, dp = 15)
    out$ascvd_full[i] <- full$ascvd
  }
}

full_digits <- function(x) ifelse(is.na(x), NA, sprintf("%.17g", x))
out$ascvd_rounded <- full_digits(out$ascvd_rounded)
out$ascvd_full <- full_digits(out$ascvd_full)
write.csv(out, args[2], row.names = FALSE)
