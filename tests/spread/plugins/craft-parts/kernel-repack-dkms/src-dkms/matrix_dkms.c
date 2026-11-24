// SPDX-License-Identifier: GPL-2.0
// Copyright (C) 2026 Canonical Ltd

#include <linux/init.h>
#include <linux/module.h>

static int __init matrix_dkms_init(void) { return 0; }
static void __exit matrix_dkms_exit(void) {}

module_init(matrix_dkms_init);
module_exit(matrix_dkms_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Kernel plugin DKMS test module");
