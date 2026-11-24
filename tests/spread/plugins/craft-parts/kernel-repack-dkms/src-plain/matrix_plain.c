// SPDX-License-Identifier: GPL-2.0
// Copyright (C) 2026 Canonical Ltd

#include <linux/init.h>
#include <linux/module.h>

static int __init matrix_plain_init(void) { return 0; }
static void __exit matrix_plain_exit(void) {}

module_init(matrix_plain_init);
module_exit(matrix_plain_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Kernel plugin prebuilt module test module");
