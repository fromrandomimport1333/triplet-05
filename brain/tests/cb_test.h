/* Minimal test harness, no framework dependency. Every check is counted and
 * the final report prints the counts. */
#ifndef CB_TEST_H
#define CB_TEST_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#include "citybrain/citybrain.h"

static int cb_t_checks = 0;
static int cb_t_failures = 0;
static int cb_t_cases = 0;
static const char *cb_t_case = "(none)";

static void cb_t_case_begin(const char *name)
{
    cb_t_case = name;
    cb_t_cases++;
}

static void cb_t_fail(const char *file, int line, const char *what)
{
    cb_t_failures++;
    fprintf(stderr, "FAIL %s:%d [%s] %s\n", file, line, cb_t_case, what);
}

#define CB_CASE(name) cb_t_case_begin(name)

#define CB_CHECK_TRUE(cond) do {                                             \
        cb_t_checks++;                                                       \
        if (!(cond)) { cb_t_fail(__FILE__, __LINE__, #cond); }               \
    } while (0)

#define CB_CHECK_STATUS(expr, expected) do {                                 \
        cb_status_t got_ = (expr);                                           \
        cb_t_checks++;                                                       \
        if (got_ != (expected)) {                                            \
            char buf_[256];                                                  \
            snprintf(buf_, sizeof buf_,                                      \
                     "%s -> status %d, expected %d", #expr,                  \
                     (int)got_, (int)(expected));                            \
            cb_t_fail(__FILE__, __LINE__, buf_);                             \
        }                                                                    \
    } while (0)

#define CB_CHECK_OK(expr) CB_CHECK_STATUS((expr), CB_OK)

#define CB_CHECK_EQ_INT(a, b) do {                                           \
        long long ga_ = (long long)(a), gb_ = (long long)(b);                \
        cb_t_checks++;                                                       \
        if (ga_ != gb_) {                                                    \
            char buf_[256];                                                  \
            snprintf(buf_, sizeof buf_, "%s == %s (%lld vs %lld)",           \
                     #a, #b, ga_, gb_);                                      \
            cb_t_fail(__FILE__, __LINE__, buf_);                             \
        }                                                                    \
    } while (0)

/* Absolute-or-relative tolerance. tol == 0.0 means bit-exact equality. */
#define CB_CHECK_NEAR(a, b, tol) do {                                        \
        double ga_ = (a), gb_ = (b), d_;                                     \
        double scale_ = fabs(gb_) > 1.0 ? fabs(gb_) : 1.0;                   \
        cb_t_checks++;                                                       \
        d_ = fabs(ga_ - gb_);                                                \
        if (!((tol) == 0.0 ? (ga_ == gb_) : (d_ <= (tol) * scale_))) {       \
            char buf_[320];                                                  \
            snprintf(buf_, sizeof buf_,                                      \
                     "%s ~= %s (%.17g vs %.17g, diff %.3g, tol %.3g)",       \
                     #a, #b, ga_, gb_, d_, (double)(tol));                   \
            cb_t_fail(__FILE__, __LINE__, buf_);                             \
        }                                                                    \
    } while (0)

static int cb_t_report(const char *suite)
{
    printf("%s: %d cases, %d checks, %d failures\n",
           suite, cb_t_cases, cb_t_checks, cb_t_failures);
    return (cb_t_failures == 0) ? 0 : 1;
}

#endif /* CB_TEST_H */
