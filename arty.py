#!/usr/bin/env python3

# This file is Copyright (c) 2015-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2020 Dolu1990 <charles.papon.90@gmail.com>
# License: BSD

import os
import argparse

from litex.soc.cores.jtag import S7JTAG
from migen import *

from litex_boards.platforms import arty

from litex.soc.cores.clock import *
from litex.soc.integration.soc import SoCRegion
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc_sdram import *
from litex.soc.integration.builder import *

from litedram.modules import MT41K128M16
from litedram.phy import s7ddrphy
from litedram.core.controller import ControllerSettings

from liteeth.phy.mii import LiteEthPHYMII

from vexriscv_smp import VexRiscvSMP

# CRG ----------------------------------------------------------------------------------------------

class _CRG(Module):
    def __init__(self, platform, sys_clk_freq):
        self.clock_domains.cd_sys       = ClockDomain()
        self.clock_domains.cd_sys4x     = ClockDomain(reset_less=True)
        self.clock_domains.cd_sys4x_dqs = ClockDomain(reset_less=True)
        self.clock_domains.cd_clk200    = ClockDomain()
        self.clock_domains.cd_eth       = ClockDomain()

        # # #

        self.submodules.pll = pll = S7PLL(speedgrade=-1)
        self.comb += pll.reset.eq(~platform.request("cpu_reset"))
        pll.register_clkin(platform.request("clk100"), 100e6)
        pll.create_clkout(self.cd_sys,       sys_clk_freq)
        pll.create_clkout(self.cd_sys4x,     4*sys_clk_freq)
        pll.create_clkout(self.cd_sys4x_dqs, 4*sys_clk_freq, phase=90)
        pll.create_clkout(self.cd_clk200,    200e6)
        pll.create_clkout(self.cd_eth,       25e6)

        self.submodules.idelayctrl = S7IDELAYCTRL(self.cd_clk200)

        self.comb += platform.request("eth_ref_clk").eq(self.cd_eth.clk)

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    interrupt_map = {
        "reserved":       0,
    }
    def __init__(self, sys_clk_freq=int(100e6), with_ethernet=False, cpu_variant="1c", **kwargs):
        platform = arty.Platform()
        platform.add_extension(arty._sdcard_pmod_io) # MicroSD PMOD on JB.

        # SoCCore ----------------------------------------------------------------------------------
        kwargs["integrated_rom_size"] = 0x10000
        SoCCore.__init__(self, platform, clk_freq=sys_clk_freq, cpu_cls=VexRiscvSMP, cpu_variant=cpu_variant, **kwargs)

        # PLIC ------------------------------------------------------------------------------------
        self.bus.add_slave("plic", self.cpu.plicbus, region=SoCRegion(origin=0xf0C00000, size=0x400000, cached=False))

        # CLINT ------------------------------------------------------------------------------------
        self.bus.add_slave("clint", self.cpu.cbus, region=SoCRegion(origin=0xf0010000, size=0x10000, cached=False))

        # CRG --------------------------------------------------------------------------------------
        self.submodules.crg = _CRG(platform, sys_clk_freq)

        # DDR3 SDRAM -------------------------------------------------------------------------------
        if not self.integrated_main_ram_size:
            self.submodules.ddrphy = s7ddrphy.A7DDRPHY(platform.request("ddram"),
                memtype      = "DDR3",
                nphases      = 4,
                sys_clk_freq = sys_clk_freq)
            self.add_csr("ddrphy")
            self.add_sdram("sdram",
                phy                     = self.ddrphy,
                module                  = MT41K128M16(sys_clk_freq, "1:4"),
                origin                  = self.mem_map["main_ram"],
                size                    = kwargs.get("max_sdram_size", 0x40000000),
                l2_cache_size           = kwargs.get("l2_size", 8192),
                l2_cache_min_data_width = kwargs.get("min_l2_data_width", 128),
                l2_cache_reverse        = True,
                controller_settings     = ControllerSettings(
                    cmd_buffer_buffered = False,
                    with_auto_precharge = False)
            )

        # Ethernet ---------------------------------------------------------------------------------
        if with_ethernet:
            self.submodules.ethphy = LiteEthPHYMII(
                clock_pads = self.platform.request("eth_clocks"),
                pads       = self.platform.request("eth"))
            self.add_csr("ethphy")
            self.add_ethernet(phy=self.ethphy)

        # JTAG ---------------------------------------------------------------------------------
        self.submodules.jtag = S7JTAG()
        self.comb += self.cpu.jtag_clk.eq(self.jtag.tck)
        self.comb += self.cpu.jtag_enable.eq(1)
        self.comb += self.cpu.jtag_capture.eq(self.jtag.capture)
        self.comb += self.cpu.jtag_shift.eq(self.jtag.shift)
        self.comb += self.cpu.jtag_update.eq(self.jtag.update)
        self.comb += self.cpu.jtag_reset.eq(self.jtag.reset)
        self.comb += self.cpu.jtag_tdi.eq(self.jtag.tdi)
        self.comb += self.jtag.tdo.eq(self.cpu.jtag_tdo)

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LiteX SoC on Nexys Video")
    parser.add_argument("--build", action="store_true", help="Build bitstream")
    parser.add_argument("--load",  action="store_true", help="Load bitstream")
    builder_args(parser)
    soc_sdram_args(parser)
    parser.add_argument("--with-ethernet", action="store_true", help="Enable Ethernet support")
    parser.add_argument("--with-sdcard",   action="store_true", help="Enable SDCard support (SPI Mode)")
    args = parser.parse_args()

    soc = BaseSoC(with_ethernet=args.with_ethernet, **soc_sdram_argdict(args))
    if args.with_sdcard:
        soc.add_spi_sdcard()
    builder = Builder(soc, compile_software=args.build, csr_json="build/arty/csr.json")
    builder.build(run=args.build)

    os.system("./json2dts.py build/arty/csr.json > build/arty/dts") # FIXME
    os.system("dtc -O dtb -o images/dtb build/arty/dts")            # FIXME

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(os.path.join(builder.gateware_dir, soc.build_name + ".bit"))

if __name__ == "__main__":
    main()
