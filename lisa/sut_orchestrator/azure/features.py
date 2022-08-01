# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import asyncio
import copy
import json
import re
from dataclasses import dataclass
from os import unlink
from pathlib import Path
from random import randint
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type, cast

import requests
import websockets
from assertpy import assert_that
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    map_error,
)
from azure.mgmt.compute.models import (  # type: ignore
    DiskCreateOption,
    DiskCreateOptionTypes,
    HardwareProfile,
    NetworkInterfaceReference,
    VirtualMachineUpdate,
)
from azure.mgmt.core.exceptions import ARMErrorFormat
from azure.mgmt.serialconsole import MicrosoftSerialConsoleClient  # type: ignore
from azure.mgmt.serialconsole.models import SerialPort, SerialPortState  # type: ignore
from azure.mgmt.serialconsole.operations import SerialPortsOperations  # type: ignore
from dataclasses_json import dataclass_json
from PIL import Image, UnidentifiedImageError
from retry import retry

from lisa import Environment, Logger, features, schema, search_space
from lisa.features.gpu import ComputeSDK
from lisa.features.resize import ResizeAction
from lisa.features.security_profile import SecurityProfileSettings, SecurityProfileType
from lisa.node import Node, RemoteNode
from lisa.operating_system import Redhat, Suse, Ubuntu
from lisa.search_space import RequirementMethod
from lisa.tools import Dmesg, Lspci, Modprobe
from lisa.util import (
    LisaException,
    NotMeetRequirementException,
    SkippedException,
    constants,
    find_patterns_in_lines,
    set_filtered_fields,
)

if TYPE_CHECKING:
    from .platform_ import AzurePlatform, AzureCapability

from .. import AZURE
from .common import (
    AzureArmParameter,
    AzureNodeSchema,
    find_by_name,
    get_compute_client,
    get_network_client,
    get_node_context,
    get_vm,
    global_credential_access_lock,
    wait_operation,
)


class AzureFeatureMixin:
    def _initialize_information(self, node: Node) -> None:
        node_context = get_node_context(node)
        self._vm_name = node_context.vm_name
        self._resource_group_name = node_context.resource_group_name


class StartStop(AzureFeatureMixin, features.StartStop):
    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        return schema.FeatureSettings.create(cls.name())

    def _stop(
        self,
        wait: bool = True,
        state: features.StopState = features.StopState.Shutdown,
    ) -> None:
        if state == features.StopState.Hibernate:
            self._execute(wait, "begin_deallocate", hibernate=True)
        else:
            self._execute(wait, "begin_deallocate")

    def _start(self, wait: bool = True) -> None:
        self._execute(wait, "begin_start")
        # on the Azure platform, after stop, start vm
        # the public ip address will change, so reload here
        self._node = cast(RemoteNode, self._node)
        platform: AzurePlatform = self._platform  # type: ignore
        public_ip = platform.load_public_ip(self._node, self._log)
        node_info = self._node.connection_info
        node_info[constants.ENVIRONMENTS_NODES_REMOTE_PUBLIC_ADDRESS] = public_ip
        self._node.set_connection_info(**node_info)
        self._node._is_initialized = False
        self._node.initialize()

    def _restart(self, wait: bool = True) -> None:
        self._execute(wait, "begin_restart")

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize(*args, **kwargs)
        self._initialize_information(self._node)

    def _execute(self, wait: bool, operator: str, **kwargs: Any) -> None:
        platform: AzurePlatform = self._platform  # type: ignore
        # The latest version may not be deployed to server side, use specified version.
        compute_client = get_compute_client(platform, api_version="2021-07-01")
        operator_method = getattr(compute_client.virtual_machines, operator)
        operation = operator_method(
            resource_group_name=self._resource_group_name,
            vm_name=self._vm_name,
            **kwargs,
        )
        if wait:
            wait_operation(operation, failure_identity="Start/Stop")


class FixedSerialPortsOperations(SerialPortsOperations):  # type: ignore
    def connect(
        self,
        resource_group_name,  # type: str
        resource_provider_namespace,  # type: str
        parent_resource_type,  # type: str
        parent_resource,  # type: str
        serial_port,  # type: str
        **kwargs,  # type: Any
    ) -> Any:
        """Connect to serial port of the target resource.
        This class overrides the Serial Ports Operations class since this package
        is incorrectly sending the POST request. It's missing the "content-type"
        field.
        """
        cls = kwargs.pop("cls", None)
        error_map = {
            401: ClientAuthenticationError,
            404: ResourceNotFoundError,
            409: ResourceExistsError,
        }
        error_map.update(kwargs.pop("error_map", {}))
        api_version = "2018-05-01"
        accept = "application/json"
        content_type = accept

        # Construct URL
        url = self.connect.metadata["url"]  # type: ignore
        path_format_arguments = {
            "resourceGroupName": self._serialize.url(
                "resource_group_name", resource_group_name, "str"
            ),
            "resourceProviderNamespace": self._serialize.url(
                "resource_provider_namespace", resource_provider_namespace, "str"
            ),
            "parentResourceType": self._serialize.url(
                "parent_resource_type", parent_resource_type, "str", skip_quote=True
            ),
            "parentResource": self._serialize.url(
                "parent_resource", parent_resource, "str"
            ),
            "serialPort": self._serialize.url("serial_port", serial_port, "str"),
            "subscriptionId": self._serialize.url(
                "self._config.subscription_id", self._config.subscription_id, "str"
            ),
        }
        url = self._client.format_url(url, **path_format_arguments)

        # Construct parameters
        query_parameters: Dict[str, Any] = {}
        query_parameters["api-version"] = self._serialize.query(
            "api_version", api_version, "str"
        )

        # Construct headers
        header_parameters: Dict[str, Any] = {}

        # Fix SerialPortsOperations: Add Content-Type header
        header_parameters["Content-Type"] = self._serialize.header(
            "content_type", content_type, "str"
        )
        header_parameters["Accept"] = self._serialize.header("accept", accept, "str")

        request = self._client.post(url, query_parameters, header_parameters)
        pipeline_response = self._client._pipeline.run(request, stream=False, **kwargs)
        response = pipeline_response.http_response

        if response.status_code not in [200]:
            map_error(  # type: ignore
                status_code=response.status_code, response=response, error_map=error_map
            )
            raise HttpResponseError(
                response=response, error_format=ARMErrorFormat
            )  # type: ignore

        deserialized = self._deserialize("SerialPortConnectResult", pipeline_response)

        if cls:
            return cls(pipeline_response, deserialized, {})

        return deserialized

    connect.metadata = {  # type: ignore
        "url": (
            "/subscriptions/{subscriptionId}/"
            "resourcegroups/{resourceGroupName}/"
            "providers/{resourceProviderNamespace}/"
            "{parentResourceType}/{parentResource}/"
            "providers/Microsoft.SerialConsole/"
            "serialPorts/{serialPort}/connect"
        )
    }


class SerialConsole(AzureFeatureMixin, features.SerialConsole):
    RESOURCE_PROVIDER_NAMESPACE = "Microsoft.Compute"
    PARENT_RESOURCE_TYPE = "virtualMachines"
    DEFAULT_SERIAL_PORT_ID = 0

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize(*args, **kwargs)
        self._initialize_information(self._node)
        self._serial_console_initialized: bool = False

    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        return schema.FeatureSettings.create(cls.name())

    @retry(tries=3, delay=5)
    def write(self, data: str) -> None:
        # websocket connection is not stable, so we need to retry
        try:
            self._write(data)
            return
        except websockets.ConnectionClosed as e:  # type: ignore
            # If the connection is closed, we need to reconnect
            self._log.debug(f"Connection closed: {e}")
            self._ws = None
            self._get_connection()
            raise e

    @retry(tries=3, delay=5)
    def read(self) -> str:
        # websocket connection is not stable, so we need to retry
        try:
            # run command with timeout
            output = self._read()
            return output
        except websockets.ConnectionClosed as e:  # type: ignore
            # If the connection is closed, we need to reconnect
            self._log.debug(f"Connection closed: {e}")
            self._ws = None
            self._get_connection()
            raise e

    def _get_event_loop(self) -> asyncio.AbstractEventLoop:
        # create asyncio loop if it doesn't exist
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def _get_connection(self) -> Any:
        if self._ws is None:
            self._log.debug("Creating connection to serial console")
            connection_str = self._get_connection_string()

            # create websocket connection
            self._ws = self._get_event_loop().run_until_complete(
                websockets.connect(connection_str)  # type: ignore
            )

        return self._ws

    def _write(self, cmd: str) -> None:
        self._initialize_serial_console(id=self.DEFAULT_SERIAL_PORT_ID)

        # connect to websocket and send command
        ws = self._get_connection()
        self._get_event_loop().run_until_complete(ws.send(cmd))

    def _read(self) -> str:
        self._initialize_serial_console(id=self.DEFAULT_SERIAL_PORT_ID)

        # connect to websocket
        ws = self._get_connection()

        # read all the available messages
        output: str = ""
        while True:
            try:
                msg = self._get_event_loop().run_until_complete(
                    asyncio.wait_for(ws.recv(), timeout=10)
                )
                output += msg
            except asyncio.TimeoutError:
                # this implies that the buffer is empty
                break

        # assert isinstance(self._output_string, str)
        if self._output_string in output:
            # implies that the connection was reset
            diff = output[len(self._output_string) :]
            self._output_string: str = output
            return diff
        else:
            self._output_string += output

        return output

    def _get_console_log(self, saved_path: Optional[Path]) -> bytes:
        platform: AzurePlatform = self._platform  # type: ignore
        compute_client = get_compute_client(platform)
        with global_credential_access_lock:
            diagnostic_data = (
                compute_client.virtual_machines.retrieve_boot_diagnostics_data(
                    resource_group_name=self._resource_group_name, vm_name=self._vm_name
                )
            )
        if saved_path:
            screenshot_raw_name = saved_path.joinpath("serial_console.bmp")
            screenshot_name = saved_path.joinpath("serial_console.png")
            screenshot_response = requests.get(
                diagnostic_data.console_screenshot_blob_uri
            )
            with open(screenshot_raw_name, mode="wb") as f:
                f.write(screenshot_response.content)
            try:

                with Image.open(screenshot_raw_name) as image:
                    image.save(screenshot_name, "PNG", optimize=True)
            except UnidentifiedImageError:
                self._log.debug(
                    "The screenshot is not generated. "
                    "The reason may be the VM is not started."
                )
            unlink(screenshot_raw_name)

        log_response = requests.get(diagnostic_data.serial_console_log_blob_uri)

        return log_response.content

    def _get_connection_string(self) -> str:
        # setup connection string
        platform: AzurePlatform = self._platform  # type: ignore
        connection = self._serial_port_operations.connect(
            resource_group_name=self._resource_group_name,
            resource_provider_namespace=self.RESOURCE_PROVIDER_NAMESPACE,
            parent_resource_type=self.PARENT_RESOURCE_TYPE,
            parent_resource=self._vm_name,
            serial_port=self._serial_port.name,
        )
        token = platform.credential.get_token("").token
        serial_port_connection_str = (
            f"{connection.connection_string}?authorization={token}"
        )

        return serial_port_connection_str

    def _initialize_serial_console(self, id: int) -> None:
        if self._serial_console_initialized:
            return

        self._serial_console_initialized = True

        platform: AzurePlatform = self._platform  # type: ignore
        with global_credential_access_lock:
            self._serial_console_client = MicrosoftSerialConsoleClient(
                credential=platform.credential, subscription_id=platform.subscription_id
            )
            self._serial_port_operations: FixedSerialPortsOperations = (
                FixedSerialPortsOperations(
                    self._serial_console_client._client,
                    self._serial_console_client._config,
                    self._serial_console_client._serialize,
                    self._serial_console_client._deserialize,
                )
            )

        # create serial port if not exists
        # list serial ports
        # https://docs.microsoft.com/en-us/python/api/azure-mgmt-serialconsole/azure.mgmt.serialconsole.operations.serialportsoperations?view=azure-python#azure-mgmt-serialconsole-operations-serialportsoperations-list
        serial_ports = self._serial_port_operations.list(
            resource_group_name=self._resource_group_name,
            resource_provider_namespace=self.RESOURCE_PROVIDER_NAMESPACE,
            parent_resource_type=self.PARENT_RESOURCE_TYPE,
            parent_resource=self._vm_name,
        )
        serial_port_ids = [int(port.name) for port in serial_ports.value]

        if id not in serial_port_ids:
            self._serial_port: SerialPort = self._serial_port_operations.create(
                resource_group_name=self._resource_group_name,
                resource_provider_namespace=self.RESOURCE_PROVIDER_NAMESPACE,
                parent_resource_type=self.PARENT_RESOURCE_TYPE,
                parent_resource=self._vm_name,
                serial_port=id,
                parameters=SerialPort(state=SerialPortState.ENABLED),
            )
        else:
            self._serial_port = [
                serialport
                for serialport in serial_ports.value
                if int(serialport.name) == id
            ][0]

        # setup shared web socket connection variable
        self._ws = None

        # setup output buffer
        self._output_string = ""


class Gpu(AzureFeatureMixin, features.Gpu):
    _grid_supported_skus = re.compile(r"^Standard_[^_]+(_v3)?$", re.I)
    _amd_supported_skus = re.compile(r"^Standard_[^_]+_v4$", re.I)
    _gpu_extension_template = """
        {
        "name": "[concat(parameters('nodes')[copyIndex('vmCopy')]['name'], '/gpu-extension')]",
        "type": "Microsoft.Compute/virtualMachines/extensions",
        "apiVersion": "2015-06-15",
        "location": "[parameters('nodes')[copyIndex('vmCopy')]['location']]",
        "copy": {
            "name": "vmCopy",
            "count": "[variables('node_count')]"
        },
        "dependsOn": [
            "[concat('Microsoft.Compute/virtualMachines/', parameters('nodes')[copyIndex('vmCopy')]['name'])]"
        ]
    }
    """  # noqa: E501
    _gpu_extension_nvidia_properties = json.loads(
        """
        {
            "publisher": "Microsoft.HpcCompute",
            "type": "NvidiaGpuDriverLinux",
            "typeHandlerVersion": "1.8",
            "autoUpgradeMinorVersion": true,
            "settings": {
            }
        }
    """
    )

    def is_supported(self) -> bool:
        # TODO: more supportability can be defined here
        node = self._node
        supported = False
        if isinstance(node.os, Redhat):
            supported = node.os.information.version >= "7.0.0"
        elif isinstance(node.os, Ubuntu):
            supported = node.os.information.version >= "18.0.0"
        elif isinstance(node.os, Suse):
            supported = node.os.information.version >= "15.0.0"

        return supported

    def get_supported_driver(self) -> List[ComputeSDK]:
        driver_list = []
        node_runbook = self._node.capability.get_extended_runbook(
            AzureNodeSchema, AZURE
        )
        if re.match(self._grid_supported_skus, node_runbook.vm_size):
            driver_list.append(ComputeSDK.GRID)
        elif re.match(self._amd_supported_skus, node_runbook.vm_size):
            driver_list.append(ComputeSDK.AMD)
            self._is_nvidia: bool = False
        else:
            driver_list.append(ComputeSDK.CUDA)

        if not driver_list:
            raise LisaException(
                "No valid Compute SDK found to install for the VM size -"
                f" {node_runbook.vm_size}."
            )
        return driver_list

    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        raw_capabilities: Any = kwargs.get("raw_capabilities")
        node_space = kwargs.get("node_space")

        assert isinstance(node_space, schema.NodeSpace), f"actual: {type(node_space)}"

        value = raw_capabilities.get("GPUs", None)
        if value:
            node_space.gpu_count = int(value)
            return schema.FeatureSettings.create(cls.name())

        return None

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize(*args, **kwargs)
        self._initialize_information(self._node)
        self._is_nvidia = True

    @classmethod
    def _install_by_platform(cls, *args: Any, **kwargs: Any) -> None:

        template: Any = kwargs.get("template")
        environment = cast(Environment, kwargs.get("environment"))
        log = cast(Logger, kwargs.get("log"))
        log.debug("updating arm template to support GPU extension.")
        resources = template["resources"]

        # load a copy to avoid side effect.
        gpu_template = json.loads(cls._gpu_extension_template)

        node: Node = environment.nodes[0]
        runbook = node.capability.get_extended_runbook(AzureNodeSchema)
        if re.match(cls._amd_supported_skus, runbook.vm_size):
            # skip AMD, because no AMD GPU Linux extension.
            ...
        else:
            gpu_template["properties"] = cls._gpu_extension_nvidia_properties
            resources.append(gpu_template)


class Infiniband(AzureFeatureMixin, features.Infiniband):
    @classmethod
    def on_before_deployment(cls, *args: Any, **kwargs: Any) -> None:
        arm_parameters: AzureArmParameter = kwargs.pop("arm_parameters")

        arm_parameters.availability_set_properties["platformFaultDomainCount"] = 1
        arm_parameters.availability_set_properties["platformUpdateDomainCount"] = 1
        arm_parameters.use_availability_sets = True

    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        raw_capabilities: Any = kwargs.get("raw_capabilities")

        value = raw_capabilities.get("RdmaEnabled", None)
        if value and eval(value) is True:
            return schema.FeatureSettings.create(cls.name())

        return None

    def is_over_sriov(self) -> bool:
        lspci = self._node.tools[Lspci]
        device_list = lspci.get_devices()
        return any("Virtual Function" in device.device_info for device in device_list)

    # nd stands for network direct
    # example SKU: Standard_H16mr
    def is_over_nd(self) -> bool:
        dmesg = self._node.tools[Dmesg]
        return "hvnd_try_bind_nic" in dmesg.get_output()

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize(*args, **kwargs)
        self._initialize_information(self._node)


class NetworkInterface(AzureFeatureMixin, features.NetworkInterface):
    """
    This Network interface feature is mainly to associate Azure
    network interface options settings.
    """

    @classmethod
    def settings_type(cls) -> Type[schema.FeatureSettings]:
        return schema.NetworkInterfaceOptionSettings

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize(*args, **kwargs)
        self._initialize_information(self._node)
        extra_nics = self._get_all_nics()
        # store extra synthetic and sriov nics count
        # in order to restore nics status after testing which needs change nics
        # extra synthetic nics count before testing
        self.origin_extra_synthetic_nics_count = len(
            [
                x
                for x in extra_nics
                if x.primary is False and x.enable_accelerated_networking is False
            ]
        )
        # extra sriov nics count before testing
        self.origin_extra_sriov_nics_count = (
            len(extra_nics) - self.origin_extra_synthetic_nics_count
        )

    def switch_sriov(self, enable: bool, wait: bool = True) -> None:
        azure_platform: AzurePlatform = self._platform  # type: ignore
        network_client = get_network_client(azure_platform)
        vm = get_vm(azure_platform, self._node)
        for nic in vm.network_profile.network_interfaces:
            # get nic name from nic id
            # /subscriptions/[subid]/resourceGroups/[rgname]/providers
            # /Microsoft.Network/networkInterfaces/[nicname]
            nic_name = nic.id.split("/")[-1]
            updated_nic = network_client.network_interfaces.get(
                self._resource_group_name, nic_name
            )
            if updated_nic.enable_accelerated_networking == enable:
                self._log.debug(
                    f"network interface {nic_name}'s accelerated networking default "
                    f"status [{updated_nic.enable_accelerated_networking}] is "
                    f"consistent with set status [{enable}], no need to update."
                )
            else:
                self._log.debug(
                    f"network interface {nic_name}'s accelerated networking default "
                    f"status [{updated_nic.enable_accelerated_networking}], "
                    f"now set its status into [{enable}]."
                )
                updated_nic.enable_accelerated_networking = enable
                network_client.network_interfaces.begin_create_or_update(
                    self._resource_group_name, updated_nic.name, updated_nic
                )
                updated_nic = network_client.network_interfaces.get(
                    self._resource_group_name, nic_name
                )
                assert_that(updated_nic.enable_accelerated_networking).described_as(
                    f"fail to set network interface {nic_name}'s accelerated "
                    f"networking into status [{enable}]"
                ).is_equal_to(enable)

        # wait settings effective
        if wait:
            self._check_sriov_enabled(enable)

    def is_enabled_sriov(self) -> bool:
        azure_platform: AzurePlatform = self._platform  # type: ignore
        network_client = get_network_client(azure_platform)
        sriov_enabled: bool = False
        vm = get_vm(azure_platform, self._node)
        nic = self._get_primary(vm.network_profile.network_interfaces)
        nic_name = nic.id.split("/")[-1]
        primary_nic = network_client.network_interfaces.get(
            self._resource_group_name, nic_name
        )
        sriov_enabled = primary_nic.enable_accelerated_networking
        return sriov_enabled

    def attach_nics(
        self, extra_nic_count: int, enable_accelerated_networking: bool = True
    ) -> None:
        if 0 == extra_nic_count:
            return
        azure_platform: AzurePlatform = self._platform  # type: ignore
        network_client = get_network_client(azure_platform)
        compute_client = get_compute_client(azure_platform)
        vm = get_vm(azure_platform, self._node)
        current_nic_count = len(vm.network_profile.network_interfaces)
        nic_count_after_add_extra = extra_nic_count + current_nic_count
        assert (
            self._node.capability.network_interface
            and self._node.capability.network_interface.max_nic_count
        )
        assert isinstance(
            self._node.capability.network_interface.max_nic_count, int
        ), f"actual: {type(self._node.capability.network_interface.max_nic_count)}"
        node_capability_nic_count = (
            self._node.capability.network_interface.max_nic_count
        )
        if nic_count_after_add_extra > node_capability_nic_count:
            raise LisaException(
                f"nic count after add extra nics is {nic_count_after_add_extra},"
                f" it exceeds the vm size's capability {node_capability_nic_count}."
            )
        nic = self._get_primary(vm.network_profile.network_interfaces)
        nic_name = nic.id.split("/")[-1]
        primary_nic = network_client.network_interfaces.get(
            self._resource_group_name, nic_name
        )

        startstop = self._node.features[StartStop]
        startstop.stop()

        network_interfaces_section = []
        index = 0
        while index < current_nic_count + extra_nic_count - 1:
            extra_nic_name = f"{self._node.name}-extra-{index}"
            self._log.debug(f"start to create the nic {extra_nic_name}.")
            params = {
                "location": vm.location,
                "enable_accelerated_networking": enable_accelerated_networking,
                "ip_configurations": [
                    {
                        "name": extra_nic_name,
                        "subnet": {"id": primary_nic.ip_configurations[0].subnet.id},
                        "primary": False,
                    }
                ],
            }
            network_client.network_interfaces.begin_create_or_update(
                resource_group_name=self._resource_group_name,
                network_interface_name=extra_nic_name,
                parameters=params,
            )
            self._log.debug(f"create the nic {extra_nic_name} successfully.")
            extra_nic = network_client.network_interfaces.get(
                network_interface_name=extra_nic_name,
                resource_group_name=self._resource_group_name,
            )

            network_interfaces_section.append({"id": extra_nic.id, "primary": False})
            index += 1
        network_interfaces_section.append({"id": primary_nic.id, "primary": True})

        self._log.debug(f"start to attach the nics into VM {self._node.name}.")
        compute_client.virtual_machines.begin_update(
            resource_group_name=self._resource_group_name,
            vm_name=self._node.name,
            parameters={
                "network_profile": {"network_interfaces": network_interfaces_section},
            },
        )
        self._log.debug(f"attach the nics into VM {self._node.name} successfully.")
        startstop.start()

    def get_nic_count(self, is_sriov_enabled: bool = True) -> int:
        return len(
            [
                x
                for x in self._get_all_nics()
                if x.enable_accelerated_networking == is_sriov_enabled
            ]
        )

    def remove_extra_nics(self) -> None:
        azure_platform: AzurePlatform = self._platform  # type: ignore
        network_client = get_network_client(azure_platform)
        compute_client = get_compute_client(azure_platform)
        vm = get_vm(azure_platform, self._node)
        if len(vm.network_profile.network_interfaces) == 1:
            self._log.debug("No existed extra nics can be disassociated.")
            return
        nic = self._get_primary(vm.network_profile.network_interfaces)
        nic_name = nic.id.split("/")[-1]
        primary_nic = network_client.network_interfaces.get(
            self._resource_group_name, nic_name
        )
        network_interfaces_section = []
        network_interfaces_section.append({"id": primary_nic.id, "primary": True})
        startstop = self._node.features[StartStop]
        startstop.stop()
        compute_client.virtual_machines.begin_update(
            resource_group_name=self._resource_group_name,
            vm_name=self._node.name,
            parameters={
                "network_profile": {"network_interfaces": network_interfaces_section},
            },
        )
        self._log.debug(
            f"Only associated nic {primary_nic.id} into VM {self._node.name}."
        )
        startstop.start()

    def reload_module(self) -> None:
        modprobe_tool = self._node.tools[Modprobe]
        modprobe_tool.reload(["hv_netvsc"])

    @retry(tries=60, delay=10)
    def _check_sriov_enabled(self, enabled: bool) -> None:
        self._node.close()
        self._node.nics.reload()
        default_nic = self._node.nics.get_nic_by_index(0)

        if enabled and not default_nic.lower:
            raise LisaException("SRIOV is enabled, but VF is not found.")
        elif not enabled and default_nic.lower:
            raise LisaException("SRIOV is disabled, but VF exists.")
        else:
            # the enabled flag is consistent with VF presents.
            ...

    def _get_primary(
        self, nics: List[NetworkInterfaceReference]
    ) -> NetworkInterfaceReference:
        found_primary = False
        for nic in nics:
            if nic.primary:
                found_primary = True
                break
        if not found_primary:
            raise LisaException(f"fail to find primary nic for vm {self._node.name}")
        return nic

    def _get_all_nics(self) -> Any:
        azure_platform: AzurePlatform = self._platform  # type: ignore
        network_client = get_network_client(azure_platform)
        vm = get_vm(azure_platform, self._node)
        all_nics = []
        for nic in vm.network_profile.network_interfaces:
            # get nic name from nic id
            # /subscriptions/[subid]/resourceGroups/[rgname]/providers
            # /Microsoft.Network/networkInterfaces/[nicname]
            nic_name = nic.id.split("/")[-1]
            all_nics.append(
                network_client.network_interfaces.get(
                    self._resource_group_name, nic_name
                )
            )
        return all_nics


# Tuple: (IOPS, Disk Size)
_disk_size_iops_map: Dict[schema.DiskType, List[Tuple[int, int]]] = {
    schema.DiskType.PremiumSSDLRS: [
        (120, 4),
        (240, 64),
        (500, 128),
        (1100, 256),
        (2300, 512),
        (5000, 1024),
        (7500, 2048),
        (16000, 8192),
        (18000, 16384),
        (20000, 32767),
    ],
    schema.DiskType.StandardHDDLRS: [
        (500, 32),
        (1300, 8192),
        (2000, 16384),
    ],
    schema.DiskType.StandardSSDLRS: [
        (500, 4),
        (2000, 8192),
        (4000, 16384),
        (6000, 32767),
    ],
}


@dataclass_json()
@dataclass()
class AzureDiskOptionSettings(schema.DiskOptionSettings):
    has_resource_disk: Optional[bool] = None

    def __hash__(self) -> int:
        return super().__hash__()

    def __str__(self) -> str:
        return self.__repr__()

    def __repr__(self) -> str:
        return f"has_resource_disk: {self.has_resource_disk},{super().__repr__()}"

    def __eq__(self, o: object) -> bool:
        assert isinstance(o, AzureDiskOptionSettings), f"actual: {type(o)}"
        return self.has_resource_disk == o.has_resource_disk and super().__eq__(o)

    # It uses to override requirement operations.
    def check(self, capability: Any) -> search_space.ResultReason:
        assert isinstance(
            capability, AzureDiskOptionSettings
        ), f"actual: {type(capability)}"
        result = super().check(capability)

        result.merge(
            search_space.check_setspace(self.disk_type, capability.disk_type),
            "disk_type",
        )
        result.merge(
            search_space.check_countspace(
                self.data_disk_count, capability.data_disk_count
            ),
            "data_disk_count",
        )
        result.merge(
            search_space.check_countspace(
                self.data_disk_iops, capability.data_disk_iops
            ),
            "data_disk_iops",
        )
        result.merge(
            search_space.check_countspace(
                self.data_disk_size, capability.data_disk_size
            ),
            "data_disk_size",
        )
        result.merge(
            self._check_has_resource_disk(
                self.has_resource_disk, capability.has_resource_disk
            ),
            "has_resource_disk",
        )
        result.merge(
            search_space.check_countspace(
                self.max_data_disk_count, capability.max_data_disk_count
            ),
            "max_data_disk_count",
        )

        return result

    def _call_requirement_method(self, method_name: str, capability: Any) -> Any:
        assert isinstance(
            capability, AzureDiskOptionSettings
        ), f"actual: {type(capability)}"

        assert (
            capability.disk_type
        ), "capability should have at least one disk type, but it's None"
        value = AzureDiskOptionSettings()
        super_value = schema.DiskOptionSettings._call_requirement_method(
            self, method_name, capability
        )
        set_filtered_fields(super_value, value, ["data_disk_count"])

        cap_disk_type = capability.disk_type
        if isinstance(cap_disk_type, search_space.SetSpace):
            assert (
                len(cap_disk_type) > 0
            ), "capability should have at least one disk type, but it's empty"
        elif isinstance(cap_disk_type, schema.DiskType):
            cap_disk_type = search_space.SetSpace[schema.DiskType](
                is_allow_set=True, items=[cap_disk_type]
            )
        else:
            raise LisaException(
                f"unknown disk type on capability, type: {cap_disk_type}"
            )

        value.disk_type = getattr(search_space, f"{method_name}_setspace_by_priority")(
            self.disk_type, capability.disk_type, schema.disk_type_priority
        )

        # below values affect data disk only.
        if self.data_disk_count is not None or capability.data_disk_count is not None:
            value.data_disk_count = getattr(search_space, f"{method_name}_countspace")(
                self.data_disk_count, capability.data_disk_count
            )

        if (
            self.max_data_disk_count is not None
            or capability.max_data_disk_count is not None
        ):
            value.max_data_disk_count = getattr(
                search_space, f"{method_name}_countspace"
            )(self.max_data_disk_count, capability.max_data_disk_count)

        # The Ephemeral doesn't support data disk, but it needs a value. And it
        # doesn't need to calculate on intersect
        value.data_disk_iops = 0
        value.data_disk_size = 0

        if method_name == RequirementMethod.generate_min_capability:
            assert isinstance(
                value.disk_type, schema.DiskType
            ), f"actual: {type(value.disk_type)}"
            disk_type_iops = _disk_size_iops_map.get(value.disk_type, None)
            # ignore unsupported disk type like Ephemeral. It supports only os
            # disk. Calculate for iops, if it has value. If not, try disk size
            if disk_type_iops:
                if isinstance(self.data_disk_iops, int) or (
                    self.data_disk_iops != search_space.IntRange(min=0)
                ):
                    req_disk_iops = search_space.count_space_to_int_range(
                        self.data_disk_iops
                    )
                    cap_disk_iops = search_space.count_space_to_int_range(
                        capability.data_disk_iops
                    )
                    min_iops = max(req_disk_iops.min, cap_disk_iops.min)
                    max_iops = min(req_disk_iops.max, cap_disk_iops.max)

                    value.data_disk_iops = min(
                        iops
                        for iops, _ in disk_type_iops
                        if iops >= min_iops and iops <= max_iops
                    )
                    value.data_disk_size = self._get_disk_size_from_iops(
                        value.data_disk_iops, disk_type_iops
                    )
                elif self.data_disk_size:
                    req_disk_size = search_space.count_space_to_int_range(
                        self.data_disk_size
                    )
                    cap_disk_size = search_space.count_space_to_int_range(
                        capability.data_disk_size
                    )
                    min_size = max(req_disk_size.min, cap_disk_size.min)
                    max_size = min(req_disk_size.max, cap_disk_size.max)

                    value.data_disk_iops = min(
                        iops
                        for iops, disk_size in disk_type_iops
                        if disk_size >= min_size and disk_size <= max_size
                    )
                    value.data_disk_size = self._get_disk_size_from_iops(
                        value.data_disk_iops, disk_type_iops
                    )
                else:
                    # if req is not specified, query minimum value.
                    cap_disk_size = search_space.count_space_to_int_range(
                        capability.data_disk_size
                    )
                    value.data_disk_iops = min(
                        iops
                        for iops, _ in disk_type_iops
                        if iops >= cap_disk_size.min and iops <= cap_disk_size.max
                    )
                    value.data_disk_size = self._get_disk_size_from_iops(
                        value.data_disk_iops, disk_type_iops
                    )
        elif method_name == RequirementMethod.intersect:
            value.data_disk_iops = search_space.intersect_countspace(
                self.data_disk_iops, capability.data_disk_iops
            )
            value.data_disk_size = search_space.intersect_countspace(
                self.data_disk_size, capability.data_disk_size
            )

        # all caching types are supported, so just take the value from requirement.
        value.data_disk_caching_type = self.data_disk_caching_type

        check_result = self._check_has_resource_disk(
            self.has_resource_disk, capability.has_resource_disk
        )
        if not check_result.result:
            raise NotMeetRequirementException("capability doesn't support requirement")
        value.has_resource_disk = capability.has_resource_disk

        return value

    def _get_disk_size_from_iops(
        self, data_disk_iops: int, disk_type_iops: List[Tuple[int, int]]
    ) -> int:
        return next(
            disk_size for iops, disk_size in disk_type_iops if iops == data_disk_iops
        )

    def _check_has_resource_disk(
        self, requirement: Optional[bool], capability: Optional[bool]
    ) -> search_space.ResultReason:
        result = search_space.ResultReason()
        # if requirement is none, capability can be either of True or False
        # else requirement should match capability
        if requirement is not None:
            if capability is None:
                result.add_reason(
                    "if requirements isn't None, capability shouldn't be None"
                )
            else:
                if requirement != capability:
                    result.add_reason(
                        "requirement is a truth value, capability should be exact "
                        f"match, requirement: {requirement}, "
                        f"capability: {capability}"
                    )

        return result

    def _get_key(self) -> str:
        return f"{super()._get_key()}/{self.has_resource_disk}"


class Disk(AzureFeatureMixin, features.Disk):
    """
    This Disk feature is mainly to associate Azure disk options settings.
    """

    @classmethod
    def settings_type(cls) -> Type[schema.FeatureSettings]:
        return AzureDiskOptionSettings

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize(*args, **kwargs)
        self._initialize_information(self._node)

    def get_raw_data_disks(self) -> List[str]:
        pattern = re.compile(r"/dev/disk/azure/scsi[0-9]/lun[0-9][0-9]?", re.M)
        # refer here to get data disks from folder /dev/disk/azure/scsi1
        # https://docs.microsoft.com/en-us/troubleshoot/azure/virtual-machines/troubleshoot-device-names-problems#identify-disk-luns  # noqa: E501
        # /dev/disk/azure/scsi1/lun0
        cmd_result = self._node.execute(
            "ls -d /dev/disk/azure/scsi1/*", shell=True, sudo=True
        )
        matched = find_patterns_in_lines(cmd_result.stdout, [pattern])
        # https://docs.microsoft.com/en-us/troubleshoot/azure/virtual-machines/troubleshoot-device-names-problems#get-the-latest-azure-storage-rules  # noqa: E501
        assert matched[0], (
            "no data disks found under folder /dev/disk/azure/scsi1/,"
            " please check data disks exist or not, also check udev"
            " rule which is to construct a set of symbolic links exist or not"
        )
        matched_disk_array = set(matched[0])
        disk_array: List[str] = [""] * len(matched_disk_array)
        for disk in matched_disk_array:
            # readlink -f /dev/disk/azure/scsi1/lun0
            # /dev/sdc
            cmd_result = self._node.execute(
                f"readlink -f {disk}", shell=True, sudo=True
            )
            disk_array[int(disk.split("/")[-1].replace("lun", ""))] = cmd_result.stdout
        # remove empty ones
        disk_array = [disk for disk in disk_array if disk != ""]
        return disk_array

    def get_all_disks(self) -> List[str]:
        # /sys/block/sda = > sda
        # /sys/block/sdb = > sdb
        disk_label_pattern = re.compile(r"/sys/block/(?P<label>sd\w*)", re.M)
        cmd_result = self._node.execute("ls -d /sys/block/sd*", shell=True, sudo=True)
        matched = find_patterns_in_lines(cmd_result.stdout, [disk_label_pattern])
        assert matched[0], "not found the matched disk label"
        return list(set(matched[0]))

    def add_data_disk(
        self,
        count: int,
        type: schema.DiskType = schema.DiskType.StandardHDDLRS,
        size_in_gb: int = 20,
    ) -> List[str]:
        disk_sku = _disk_type_mapping.get(type, None)
        assert disk_sku
        assert self._node.capability.disk
        assert isinstance(self._node.capability.disk.data_disk_count, int)
        current_disk_count = self._node.capability.disk.data_disk_count
        platform: AzurePlatform = self._platform  # type: ignore
        compute_client = get_compute_client(platform)
        node_context = self._node.capability.get_extended_runbook(AzureNodeSchema)

        # create managed disk
        managed_disks = []
        for i in range(count):
            name = f"lisa_data_disk_{i+current_disk_count}"
            async_disk_update = compute_client.disks.begin_create_or_update(
                self._resource_group_name,
                name,
                {
                    "location": node_context.location,
                    "disk_size_gb": size_in_gb,
                    "sku": {"name": disk_sku},
                    "creation_data": {"create_option": DiskCreateOption.empty},
                },
            )
            managed_disks.append(async_disk_update.result())

        # attach managed disk
        azure_platform: AzurePlatform = self._platform  # type: ignore
        vm = get_vm(azure_platform, self._node)
        for i, managed_disk in enumerate(managed_disks):
            lun = str(i + current_disk_count)
            vm.storage_profile.data_disks.append(
                {
                    "lun": lun,
                    "name": managed_disk.name,
                    "create_option": DiskCreateOptionTypes.attach,
                    "managed_disk": {"id": managed_disk.id},
                }
            )

        # update vm
        async_vm_update = compute_client.virtual_machines.begin_create_or_update(
            self._resource_group_name,
            vm.name,
            vm,
        )
        async_vm_update.wait()

        # update data disk count
        add_disk_names = [managed_disk.name for managed_disk in managed_disks]
        self.disks += add_disk_names
        self._node.capability.disk.data_disk_count += len(managed_disks)

        return add_disk_names

    def remove_data_disk(self, names: Optional[List[str]] = None) -> None:
        assert self._node.capability.disk
        platform: AzurePlatform = self._platform  # type: ignore
        compute_client = get_compute_client(platform)

        # if names is None, remove all data disks
        if names is None:
            names = self.disks

        # detach managed disk
        azure_platform: AzurePlatform = self._platform  # type: ignore
        vm = get_vm(azure_platform, self._node)

        # remove managed disk
        data_disks = vm.storage_profile.data_disks
        data_disks[:] = [disk for disk in data_disks if disk.name not in names]

        # update vm
        async_vm_update = compute_client.virtual_machines.begin_create_or_update(
            self._resource_group_name,
            vm.name,
            vm,
        )
        async_vm_update.wait()

        # delete managed disk
        for name in names:
            async_disk_delete = compute_client.disks.begin_delete(
                self._resource_group_name, name
            )
            async_disk_delete.wait()

        # update data disk count
        assert isinstance(self._node.capability.disk.data_disk_count, int)
        self.disks = [name for name in self.disks if name not in names]
        self._node.capability.disk.data_disk_count -= len(names)
        self._node.close()


def get_azure_disk_type(disk_type: schema.DiskType) -> str:
    assert isinstance(disk_type, schema.DiskType), (
        "the disk_type must be one value when calling get_disk_type. "
        f"But it's {disk_type}"
    )

    result = _disk_type_mapping.get(disk_type, None)
    assert result, f"unknown disk type: {disk_type}"

    return result


_disk_type_mapping: Dict[schema.DiskType, str] = {
    schema.DiskType.PremiumSSDLRS: "Premium_LRS",
    schema.DiskType.Ephemeral: "Ephemeral",
    schema.DiskType.StandardHDDLRS: "Standard_LRS",
    schema.DiskType.StandardSSDLRS: "StandardSSD_LRS",
}


class Resize(AzureFeatureMixin, features.Resize):
    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        return schema.FeatureSettings.create(cls.name())

    def resize(
        self, resize_action: ResizeAction = ResizeAction.IncreaseCoreCount
    ) -> schema.NodeSpace:
        platform: AzurePlatform = self._platform  # type: ignore
        compute_client = get_compute_client(platform)
        node_context = get_node_context(self._node)
        new_vm_size_info = self._select_vm_size(resize_action)

        # Creating parameter for VM Operations API call
        hardware_profile = HardwareProfile(vm_size=new_vm_size_info.vm_size)
        vm_update = VirtualMachineUpdate(hardware_profile=hardware_profile)

        # Resizing with new Vm Size
        lro_poller = compute_client.virtual_machines.begin_update(
            resource_group_name=node_context.resource_group_name,
            vm_name=node_context.vm_name,
            parameters=vm_update,
        )

        # Waiting for the Long Running Operation to finish
        wait_operation(lro_poller, time_out=1200)

        self._node.close()
        new_capability = copy.deepcopy(new_vm_size_info.capability)
        self._node.capability = cast(schema.Capability, new_capability)
        return new_capability

    def _select_vm_size(
        self, resize_action: ResizeAction = ResizeAction.IncreaseCoreCount
    ) -> "AzureCapability":
        platform: AzurePlatform = self._platform  # type: ignore
        compute_client = get_compute_client(platform)
        node_context = get_node_context(self._node)
        node_runbook = self._node.capability.get_extended_runbook(AzureNodeSchema)

        # Get list of vm sizes that the current sku can resize to
        available_sizes = compute_client.virtual_machines.list_available_sizes(
            node_context.resource_group_name, node_context.vm_name
        )
        # Get list of vm sizes available in the current location
        location_info = platform.get_location_info(node_runbook.location, self._log)
        capabilities = [value for _, value in location_info.capabilities.items()]
        sorted_sizes = platform.get_sorted_vm_sizes(capabilities, self._log)

        current_vm_size = next(
            (x for x in sorted_sizes if x.vm_size == node_runbook.vm_size),
            None,
        )
        assert current_vm_size, "cannot find current vm size in eligible list"

        # Intersection of available_sizes and eligible_sizes
        avail_eligible_intersect: List[AzureCapability] = []
        # Populating avail_eligible_intersect with vm sizes that are available in the
        # current location and that are available for the current vm size to resize to
        for size in available_sizes:
            vm_size_name = size.as_dict()["name"]
            # Getting eligible vm sizes and their capability data
            new_vm_size = next(
                (x for x in sorted_sizes if x.vm_size == vm_size_name), None
            )
            if not new_vm_size:
                continue

            avail_eligible_intersect.append(new_vm_size)

        current_network_interface = current_vm_size.capability.network_interface
        assert_that(current_network_interface).described_as(
            "current_network_interface is not of type NetworkInterfaceOptionSettings."
        ).is_instance_of(schema.NetworkInterfaceOptionSettings)
        current_data_path = current_network_interface.data_path  # type: ignore
        current_core_count = current_vm_size.capability.core_count
        assert_that(current_core_count).described_as(
            "Didn't return an integer to represent the current VM size core count."
        ).is_instance_of(int)
        # Loop removes candidate vm sizes if they can't be resized to or if the
        # change in cores resulting from the resize is undesired
        for candidate_size in avail_eligible_intersect[:]:
            candidate_network_interface = candidate_size.capability.network_interface
            assert_that(candidate_network_interface).described_as(
                "candidate_network_interface is not of type "
                "NetworkInterfaceOptionSettings."
            ).is_instance_of(schema.NetworkInterfaceOptionSettings)
            candidate_data_path = candidate_network_interface.data_path  # type: ignore
            # Can't resize from an accelerated networking enabled size to a size where
            # accelerated networking isn't enabled
            if (
                schema.NetworkDataPath.Sriov in current_data_path  # type: ignore
                and schema.NetworkDataPath.Sriov not in candidate_data_path  # type: ignore # noqa: E501
            ):
                # Removing sizes without accelerated networking capabilities
                # if original size has it enabled
                avail_eligible_intersect.remove(candidate_size)
                continue

            candidate_core_count = candidate_size.capability.core_count
            assert_that(candidate_core_count).described_as(
                "Didn't return an integer to represent the "
                "candidate VM size core count."
            ).is_instance_of(int)
            # Removing vm size from candidate list if the change in core count
            # doesn't align with the ResizeAction passed into this function
            if (
                resize_action == ResizeAction.IncreaseCoreCount
                and candidate_core_count <= current_core_count  # type: ignore
                or resize_action == ResizeAction.DecreaseCoreCount
                and candidate_core_count >= current_core_count  # type: ignore
            ):
                avail_eligible_intersect.remove(candidate_size)

        if not avail_eligible_intersect:
            raise LisaException(
                f"current vm size: {current_vm_size.vm_size},"
                f" no available size for resizing with {resize_action} setting."
            )

        # Choose random size from the list to resize to
        index = randint(0, len(avail_eligible_intersect) - 1)
        resize_vm_size = avail_eligible_intersect[index]
        self._log.info(f"New vm size: {resize_vm_size.vm_size}")
        return resize_vm_size


class Hibernation(AzureFeatureMixin, features.Hibernation):
    _hibernation_properties = """
        {
            "additionalCapabilities": {
                "hibernationEnabled": "true"
            }
        }
        """

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize(*args, **kwargs)
        self._initialize_information(self._node)

    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        raw_capabilities: Any = kwargs.get("raw_capabilities")

        value = raw_capabilities.get("HibernationSupported", None)
        if value and eval(value) is True:
            return schema.FeatureSettings.create(cls.name())

        return None

    @classmethod
    def _enable_hibernation(cls, *args: Any, **kwargs: Any) -> None:
        parameters: Any = kwargs.get("arm_parameters")
        if parameters.use_availability_sets:
            raise SkippedException(
                "Hibernation cannot be enabled on Virtual Machines created in an"
                " Availability Set."
            )
        template: Any = kwargs.get("template")
        log = cast(Logger, kwargs.get("log"))
        log.debug("updating arm template to support vm hibernation.")
        resources = template["resources"]
        virtual_machines = find_by_name(resources, "Microsoft.Compute/virtualMachines")
        virtual_machines["properties"].update(json.loads(cls._hibernation_properties))


class SecurityProfile(AzureFeatureMixin, features.SecurityProfile):
    _both_enabled_properties = """
        {
            "securityProfile": {
                "uefiSettings": {
                    "secureBootEnabled": "true",
                    "vTpmEnabled": "true"
                },
                "securityType": "%s"
            }
        }
        """

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        super()._initialize(*args, **kwargs)
        self._initialize_information(self._node)

    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        raw_capabilities: Any = kwargs.get("raw_capabilities")
        capabilities: set[SecurityProfileType] = {SecurityProfileType.Standard}

        gen_value = raw_capabilities.get("HyperVGenerations", None)
        cvm_value = raw_capabilities.get("ConfidentialComputingType", None)
        if gen_value and "V2" in str(gen_value):
            capabilities.add(SecurityProfileType.Boot)
        if cvm_value:
            capabilities.add(SecurityProfileType.CVM)

        return SecurityProfileSettings(
            security_profile=search_space.SetSpace(True, capabilities)
        )

    @classmethod
    def on_before_deployment(cls, *args: Any, **kwargs: Any) -> None:
        settings = cast(SecurityProfileSettings, kwargs.get("settings"))
        runbook: Any = cast(Any, kwargs.get("arm_parameters")).nodes[0]
        try:
            if runbook.security_profile:
                runbook_settings = SecurityProfileSettings(
                    security_profile=search_space.SetSpace(
                        items=[SecurityProfileType(runbook.security_profile)]
                    )
                )
                settings = settings.generate_min_capability(runbook_settings)
        except ValueError:
            raise LisaException(
                f"Bad runbook value for secuirty profile: {runbook.security_profile}"
            )
        if SecurityProfileType.Standard not in settings.security_profile:
            cls._enable_secure_boot(*args, **kwargs)

    @classmethod
    def _enable_secure_boot(cls, *args: Any, **kwargs: Any) -> None:
        parameters: Any = kwargs.get("arm_parameters")
        settings: Any = kwargs.get("settings")
        if 1 == parameters.nodes[0].hyperv_generation:
            raise SkippedException("Secure boot can only be set on gen2 image/vhd.")
        template: Any = kwargs.get("template")
        log = cast(Logger, kwargs.get("log"))
        resources = template["resources"]
        virtual_machines = find_by_name(resources, "Microsoft.Compute/virtualMachines")
        if SecurityProfileType.Standard in settings.security_profile:
            log.debug("Security profile set to none. Arm template will not be updated.")
            return
        elif SecurityProfileType.Boot in settings.security_profile:
            log.debug("Security Profile set to secure boot. Updating arm template.")
            security_type = "TrustedLaunch"
        elif SecurityProfileType.CVM in settings.security_profile:
            log.debug("Security Profile set to CVM. Updating arm template.")
            security_type = "ConfidentialVM"

            virtual_machines["properties"]["storageProfile"]["osDisk"][
                "managedDisk"
            ] = (
                "[if(not(equals(parameters('nodes')[copyIndex('vmCopy')]['disk_type'], "
                "'Ephemeral')), json(concat('{\"storageAccountType\": \"',parameters"
                "('nodes')[copyIndex('vmCopy')]['disk_type'],'\",\"securityProfile\":{"
                '"securityEncryptionType": "VMGuestStateOnly"}}\')), json(\'null\'))]'
            )
        else:
            raise LisaException(
                "Security profile: not all requirements could be met. "
                "Please check VM SKU capabilities, test requirements, "
                "and runbook requirements."
            )

        virtual_machines["properties"].update(
            json.loads(
                cls._both_enabled_properties % security_type,
            )
        )


class IsolatedResource(AzureFeatureMixin, features.IsolatedResource):
    # From https://docs.microsoft.com/en-us/azure/security/fundamentals/isolation-choices#compute-isolation # noqa: E501
    supported_vm_sizes = set(
        [
            "Standard_E80ids_v4",
            "Standard_E80is_v4",
            "Standard_E104i_v5",
            "Standard_E104is_v5",
            "Standard_E104id_v5",
            "Standard_E104ids_v5",
            "Standard_M192is_v2",
            "Standard_M192ims_v2",
            "Standard_M192ids_v2",
            "Standard_M192idms_v2",
            "Standard_F72s_v2",
            "Standard_M128ms",
            # add custom vm sizes below,
        ]
    )

    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        resource_sku: Any = kwargs.get("resource_sku")

        if resource_sku.name in cls.supported_vm_sizes:
            return schema.FeatureSettings.create(cls.name())

        return None


class ACC(AzureFeatureMixin, features.ACC):
    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        resource_sku: Any = kwargs.get("resource_sku")

        if resource_sku.family in ["standardDCSv2Family", "standardDCSv3Family"]:
            return schema.FeatureSettings.create(cls.name())
        return None


class NestedVirtualization(AzureFeatureMixin, features.NestedVirtualization):
    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        resource_sku: Any = kwargs.get("resource_sku")

        # add vm which support nested virtualization
        # https://docs.microsoft.com/en-us/azure/virtual-machines/acu
        if resource_sku.family in [
            "standardDv3Family",
            "standardDSv3Family",
            "standardDv4Family",
            "standardDSv4Family",
            "standardDDv4Family",
            "standardDDSv4Family",
            "standardEv3Family",
            "standardESv3Family",
            "standardEv4Family",
            "standardESv4Family",
            "standardEDv4Family",
            "standardEDSv4Family",
            "standardFSv2Family",
            "standardMSFamily",
            "standardMSMediumMemoryv2Family",
        ]:
            return schema.FeatureSettings.create(cls.name())
        return None


class Nvme(AzureFeatureMixin, features.Nvme):
    @classmethod
    def create_setting(
        cls, *args: Any, **kwargs: Any
    ) -> Optional[schema.FeatureSettings]:
        resource_sku: Any = kwargs.get("resource_sku")
        node_space: Any = kwargs.get("node_space")

        assert isinstance(node_space, schema.NodeSpace), f"actual: {type(node_space)}"
        # add vm which support nested virtualization
        # https://docs.microsoft.com/en-us/azure/virtual-machines/acu
        if resource_sku.family in [
            "standardLSv2Family",
        ]:
            # refer https://docs.microsoft.com/en-us/azure/virtual-machines/lsv2-series # noqa: E501
            # NVMe disk count = vCPU / 8
            nvme = features.NvmeSettings()
            assert isinstance(
                node_space.core_count, int
            ), f"actual: {node_space.core_count}"
            nvme.disk_count = int(node_space.core_count / 8)
            return nvme

        return None
