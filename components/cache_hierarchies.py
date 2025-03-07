# Copyright (c) 2021 The Regents of the University of California
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


from gem5.components.cachehierarchies.ruby.\
    abstract_ruby_cache_hierarchy import AbstractRubyCacheHierarchy
from gem5.components.cachehierarchies.\
    abstract_two_level_cache_hierarchy import AbstractTwoLevelCacheHierarchy
from gem5.coherence_protocol import CoherenceProtocol
from gem5.isas import ISA
from gem5.components.boards.abstract_board import AbstractBoard
from gem5.utils.requires import requires

from .network import L1L2ClusterTree
from gem5.components.cachehierarchies.ruby.\
    caches.mesi_two_level.l1_cache import L1Cache
from gem5.components.cachehierarchies.ruby.\
    caches.mesi_two_level.l2_cache import L2Cache
from gem5.components.cachehierarchies.ruby.\
    caches.mesi_two_level.directory import Directory
from gem5.components.cachehierarchies.ruby.\
    caches.mesi_two_level.dma_controller import DMAController

from m5.objects import (
    RubySystem,
    RubySequencer,
    DMASequencer,
    RubyPortProxy,
)

class MESITwoLevelCacheHierarchy(
    AbstractRubyCacheHierarchy, AbstractTwoLevelCacheHierarchy
):
    """A two level private L1 shared L2 MESI hierarchy.

    In addition to the normal two level parameters, you can also change the
    number of L2 banks in this protocol.

    The on-chip network is a crossbar with a configurable latency.
    """

    def __init__(self, xbar_latency: int):
        AbstractRubyCacheHierarchy.__init__(self=self)
        AbstractTwoLevelCacheHierarchy.__init__(
            self,
            l1i_size="32KiB",
            l1i_assoc=8,
            l1d_size="32KiB",
            l1d_assoc=8,
            l2_size="512KiB",
            l2_assoc=8,
        )

        self._xbar_latency = xbar_latency

    def incorporate_cache(self, board: AbstractBoard) -> None:

        requires(coherence_protocol_required=CoherenceProtocol.MESI_TWO_LEVEL)

        cache_line_size = board.get_cache_line_size()

        self.ruby_system = RubySystem()

        # MESI_Two_Level needs 5 virtual networks
        self.ruby_system.number_of_virtual_networks = 5

        self.ruby_system.network = L1L2ClusterTree(
            self.ruby_system, self._xbar_latency
        )
        self.ruby_system.network.number_of_virtual_networks = 5

        self._num_l2_banks = board.get_processor().get_actual_num_cores()
        runtime_isa = board.get_processor().get_isa()

        self._l1_controllers = []
        for i, core in enumerate(board.get_processor().get_cores()):
            cache = L1Cache(
                self._l1i_size,
                self._l1i_assoc,
                self._l1d_size,
                self._l1d_assoc,
                self.ruby_system.network,
                core,
                self._num_l2_banks,
                cache_line_size,
                runtime_isa,
                board.get_clock_domain(),
            )

            cache.sequencer = RubySequencer(
                version=i,
                dcache=cache.L1Dcache,
                clk_domain=cache.clk_domain,
                ruby_system=self.ruby_system,
            )

            if board.has_io_bus():
                cache.sequencer.connectIOPorts(board.get_io_bus())

            cache.ruby_system = self.ruby_system

            core.connect_icache(cache.sequencer.in_ports)
            core.connect_dcache(cache.sequencer.in_ports)

            core.connect_walker_ports(
                cache.sequencer.in_ports, cache.sequencer.in_ports
            )

            # Connect the interrupt ports
            if runtime_isa == ISA.X86:
                int_req_port = cache.sequencer.interrupt_out_port
                int_resp_port = cache.sequencer.in_ports
                core.connect_interrupt(int_req_port, int_resp_port)
            else:
                core.connect_interrupt()

            self._l1_controllers.append(cache)

        self._l2_controllers = [
            L2Cache(
                self._l2_size,
                self._l2_assoc,
                self.ruby_system.network,
                self._num_l2_banks,
                cache_line_size,
            )
            for _ in range(self._num_l2_banks)
        ]
        # TODO: Make this prettier: The problem is not being able to proxy
        # the ruby system correctly
        for cache in self._l2_controllers:
            cache.ruby_system = self.ruby_system

        self._directory_controllers = [
            Directory(self.ruby_system.network, cache_line_size, range, port)
            for range, port in board.get_memory().get_mem_ports()
        ]
        # TODO: Make this prettier: The problem is not being able to proxy
        # the ruby system correctly
        for dir in self._directory_controllers:
            dir.ruby_system = self.ruby_system

        self._dma_controllers = []
        if board.has_dma_ports():
            dma_ports = board.get_dma_ports()
            for i, port in enumerate(dma_ports):
                ctrl = DMAController(self.ruby_system.network, cache_line_size)
                ctrl.dma_sequencer = DMASequencer(version=i, in_ports=port)
                self._dma_controllers.append(ctrl)
                ctrl.ruby_system = self.ruby_system

        self.ruby_system.num_of_sequencers = len(self._l1_controllers) + len(
            self._dma_controllers
        )
        self.ruby_system.l1_controllers = self._l1_controllers
        self.ruby_system.l2_controllers = self._l2_controllers
        self.ruby_system.directory_controllers = self._directory_controllers

        if len(self._dma_controllers) != 0:
            self.ruby_system.dma_controllers = self._dma_controllers

        # Create the network and connect the controllers.
        self.ruby_system.network.connectControllers(
            self._l1_controllers,
            self._l2_controllers,
            self._directory_controllers[0],
        )

        self.ruby_system.network.setup_buffers()

        # Set up a proxy port for the system_port. Used for load binaries and
        # other functional-only things.
        self.ruby_system.sys_port_proxy = RubyPortProxy(
            ruby_system=self.ruby_system
        )
        board.connect_system_port(self.ruby_system.sys_port_proxy.in_ports)
