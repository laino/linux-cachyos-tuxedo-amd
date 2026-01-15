Unofficial Linux Cachyos TUXEDO AMD Kernel PKGBUILD
====================================================

PKGBUILD for building a custom Linux kernel optimized for modern AMD hardware,
based on the Cachyos kernel configuration with [TUXEDO Computer's](https://gitlab.com/tuxedocomputers/development/packages/linux) changes applied.

Not all changes from TUXEDO are included, only those that will apply to the current Cachyos kernel version.

Contains a script (scripts/generate_package.py) to generate a PKGBUILD by fetching CachyOS' and TUXEDO's kernel sources, extracting TUXEDO's
changes from the latter (they're based on Ubuntu's kernel and we don't want all of that) and applying them to CachyOS' kernel source tree.

Install / Usage
---------------

Clone this repository and use makepkg to build the kernel package:

```bash
git clone https://github.com/laino/linux-cachyos-tuxedo-amd.git
cd package
makepkg -Cfsi
```