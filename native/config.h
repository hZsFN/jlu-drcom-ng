/* Loads config.json as written by the Python build (DPAPI password included). */
#ifndef DRCOMD_CONFIG_H
#define DRCOMD_CONFIG_H

#include <stddef.h>
#include "engine.h"

/*
 * Populate *cfg from the shared config.json.  On failure returns -1 and
 * writes a human-readable reason into err.
 * api_port_out (optional) receives the status-API port from the "api" section.
 */
int drcom_config_load(const char *path, engine_config *cfg,
                      char *err, size_t errcap, int *api_port_out);

/* Decrypt a password_protected value ("D1:" DPAPI envelope). */
int decrypt_password(const char *stored, char *out, size_t cap,
                     char *err, size_t errcap);

#endif
