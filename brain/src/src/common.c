#include "citybrain/common.h"
#include "internal.h"

/* Stringify without relying on any mutable state. */
#define CB_STR_HELPER(x) #x
#define CB_STR(x) CB_STR_HELPER(x)
static const char cb_version_text[] =
    CB_STR(CB_VERSION_MAJOR) "." CB_STR(CB_VERSION_MINOR) "." CB_STR(CB_VERSION_PATCH);

cb_status_t cb_status_message(cb_status_t status, const char **out_message)
{
    const char *msg;

    CB_REQUIRE(out_message != NULL, CB_ERR_NULL);

    switch (status) {
    case CB_OK:              msg = "ok"; break;
    case CB_ERR_NULL:        msg = "required pointer argument was NULL"; break;
    case CB_ERR_DOMAIN:      msg = "argument outside its documented domain"; break;
    case CB_ERR_NOT_FINITE:  msg = "argument was NaN or infinite"; break;
    case CB_ERR_CAPACITY:    msg = "count or buffer size outside supported range"; break;
    case CB_ERR_STATE:       msg = "state not initialised or internally inconsistent"; break;
    case CB_ERR_NOT_FOUND:   msg = "requested element does not exist"; break;
    case CB_ERR_NO_SOLUTION: msg = "problem has no feasible solution"; break;
    case CB_ERR_NO_CONVERGE: msg = "iteration limit reached before tolerance met"; break;
    case CB_ERR_UNSUPPORTED: msg = "operation not supported by this build"; break;
    default:                 msg = "unknown status code"; break;
    }
    *out_message = msg;
    return CB_OK;
}

cb_status_t cb_version_string(const char **out_text)
{
    CB_REQUIRE(out_text != NULL, CB_ERR_NULL);
    *out_text = cb_version_text;
    return CB_OK;
}

cb_status_t cb_version_numbers(int32_t *out_major, int32_t *out_minor, int32_t *out_patch)
{
    if (out_major != NULL) { *out_major = (int32_t)CB_VERSION_MAJOR; }
    if (out_minor != NULL) { *out_minor = (int32_t)CB_VERSION_MINOR; }
    if (out_patch != NULL) { *out_patch = (int32_t)CB_VERSION_PATCH; }
    return CB_OK;
}
