/*
 * Common definitions shared by every module. The library is C99, has no
 * network or file I/O, no global mutable state, and every function returns
 * a status code instead of throwing or crashing on bad input.
 */
#ifndef CITYBRAIN_COMMON_H
#define CITYBRAIN_COMMON_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32) && defined(CITYBRAIN_BUILD_SHARED)
#  define CB_API __declspec(dllexport)
#elif defined(_WIN32) && defined(CITYBRAIN_USE_SHARED)
#  define CB_API __declspec(dllimport)
#else
#  define CB_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define CB_VERSION_MAJOR 1
#define CB_VERSION_MINOR 1
#define CB_VERSION_PATCH 0

/* Status codes returned by every function in this library. CB_OK is 0;
 * anything else is an error and leaves output arguments untouched unless a
 * function says otherwise. */
typedef enum cb_status {
    CB_OK             = 0,
    CB_ERR_NULL       = 1, /* a required pointer argument was NULL */
    CB_ERR_DOMAIN     = 2, /* argument outside its valid range */
    CB_ERR_NOT_FINITE = 3, /* argument was NaN or infinite */
    CB_ERR_CAPACITY   = 4, /* count or buffer size too large */
    CB_ERR_STATE      = 5, /* struct not initialised or inconsistent */
    CB_ERR_NOT_FOUND  = 6, /* requested element does not exist */
    CB_ERR_NO_SOLUTION= 7, /* problem has no feasible solution */
    CB_ERR_NO_CONVERGE= 8, /* iteration limit reached before converging */
    CB_ERR_UNSUPPORTED= 9  /* valid request this build does not implement */
} cb_status_t;

/* Human-readable message for a status code. Works for any int value. */
CB_API cb_status_t cb_status_message(cb_status_t status, const char **out_message);

/* Library version as "MAJOR.MINOR.PATCH". */
CB_API cb_status_t cb_version_string(const char **out_text);

/* Library version as three integers. Any pointer may be NULL. */
CB_API cb_status_t cb_version_numbers(int32_t *out_major,
                                      int32_t *out_minor,
                                      int32_t *out_patch);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_COMMON_H */
