# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import re
from typing import Dict, List, Optional

from lisa.executable import Tool
from lisa.tools import Cat


class Interrupt:
    irq_number: str
    cpu_counter: List[int]
    metadata: str
    counter_sum: int

    def __init__(
        self,
        irq_number: str,
        cpu_counter: List[int],
        counter_sum: int,
        metadata: str = "",
    ) -> None:
        self.irq_number = irq_number
        self.cpu_counter = cpu_counter
        self.metadata = metadata
        self.counter_sum = counter_sum

    def __str__(self) -> str:
        return (
            f"irq_number : {self.irq_number}, "
            f"count : {self.cpu_counter}, "
            f"metadata : {self.metadata}"
            f"sum : {self.counter_sum}"
        )

    def __repr__(self) -> str:
        return self.__str__()


class InterruptInspector(Tool):
    # 0:         22          0  IR-IO-APIC   2-edge      timer
    _interrupt_regex = re.compile(
        r"^\s*(?P<irq_number>\S+):\s+(?P<cpu_counter>[\d+ ]+)\s*(?P<metadata>.*)$"
    )

    @property
    def command(self) -> str:
        return "cat /proc/interrupts"

    @property
    def can_install(self) -> bool:
        return False

    def get_interrupt_data(self) -> List[Interrupt]:
        # Run cat /proc/interrupts. The output is of the form :
        #          CPU0       CPU1
        # 0:         22          0  IR-IO-APIC   2-edge      timer
        # 1:          2          0  IR-IO-APIC   1-edge      i8042
        # ERR:        0
        # The first column refers to the IRQ number. The next column contains
        # number of interrupts per IRQ for each CPU in the system. The remaining
        # column report the metadata about interrupts, including type of interrupt,
        # device etc. This is variable for each distro.
        # Note : Some IRQ numbers have single entry because they're not actually
        # CPU stats, but events count belonging to the IO-APIC controller. For
        # example, `ERR` is incremented in the case of errors in the IO-APIC bus.
        result = (
            self.node.tools[Cat]
            .run("/proc/interrupts", sudo=True, force_run=True)
            .stdout
        )
        mappings_with_header = result.splitlines(keepends=False)
        mappings = mappings_with_header[1:]
        assert len(mappings) > 0

        interrupts = []
        for line in mappings:
            matched = self._interrupt_regex.fullmatch(line)
            assert matched
            cpu_counter = [int(count) for count in matched.group("cpu_counter").split()]
            counter_sum = sum([int(x) for x in cpu_counter])
            interrupts.append(
                Interrupt(
                    irq_number=matched.group("irq_number"),
                    cpu_counter=cpu_counter,
                    counter_sum=counter_sum,
                    metadata=matched.group("metadata"),
                )
            )

        return interrupts

    def sum_cpu_counter_by_irqs(
        self,
        pci_slot: str,
        exclude_key_words: Optional[List[str]] = None,
    ) -> List[Dict[str, int]]:
        interrupts_sum_by_irqs: List[Dict[str, int]] = []
        interrupts = self.get_interrupt_data()
        if exclude_key_words is None:
            exclude_key_words = []
        matched_interrupts = [
            x
            for x in interrupts
            if pci_slot in x.metadata
            and all(y not in x.metadata for y in exclude_key_words)
        ]
        for interrupt in matched_interrupts:
            interrupts_sum_by_irqs.append({interrupt.irq_number: interrupt.counter_sum})
        return interrupts_sum_by_irqs

    def sum_cpu_counter_by_index(self, pci_slot: str) -> Dict[int, int]:
        interrupts_sum_by_cpus: Dict[int, int] = {}
        interrupts = self.get_interrupt_data()
        matched_interrupts = [x for x in interrupts if pci_slot in x.metadata]
        for cpu_index in range(0, len(matched_interrupts[0].cpu_counter)):
            interrupts_sum_by_cpus[cpu_index] = self._get_sum_of_interrupt_data_per_cpu(
                matched_interrupts, cpu_index
            )
        return interrupts_sum_by_cpus

    def _get_sum_of_interrupt_data_per_cpu(
        self, interrupts: List[Interrupt], index: int
    ) -> int:
        sum = 0
        for interrupt in interrupts:
            sum += interrupt.cpu_counter[index]
        return sum
