# conding=utf-8
import logging
import struct
from operator import attrgetter
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller import controller
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_0
from ryu.ofproto import ofproto_v1_0_parser

from ryu.ofproto import ofproto_v1_3
from ryu.ofproto import ofproto_v1_3_parser
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ipv4
from ryu.lib.packet import arp

from ryu.topology import event, switches
from ryu.topology.api import get_switch, get_link

from ryu.openexchange.network import network_aware
from ryu.openexchange.network import network_monitor

from ryu.openexchange import oxproto_v1_0
from ryu.openexchange import oxproto_common
from ryu.openexchange.routing_algorithm.routing_algorithm import get_paths
from ryu.openexchange.utils import utils
from ryu.openexchange.event import oxp_event


class Routing(app_manager.RyuApp):
    def __init__(self, *args, **kwargs):
        super(Routing, self).__init__(*args, **kwargs)
        self.module_topo = app_manager.lookup_service_brick('oxp_topology')
        self.topology = self.module_topo.topo
        self.location = self.module_topo.location
        self.domains = {}
        self.graph = {}
        self.paths = {}
        self.capabilities = {}

    @set_ev_cls(oxp_event.EventOXPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        domain = ev.domain
        if ev.state == MAIN_DISPATCHER:
            if domain.id not in self.domains.keys():
                self.domains.setdefault(domain.id, None)
                self.domains[domain.id] = domain
        if ev.state == DEAD_DISPATCHER:
            del self.domains[domain.id]

    def get_host_location(self, host_ip):
        for domain_id in self.location.locations:
            if host_ip in self.location.locations[domain_id]:
                return domain_id
        self.logger.debug("%s location is not found." % host_ip)
        return None

    # get Adjacency matrix from inter-links.
    def get_graph(self, link_list, nodes):
        graph = {}
        for src in nodes.keys():
            for dst in nodes.keys():
                graph.setdefault(src, {dst: float('inf')})
                graph[src][dst] = float('inf')
                if src == dst:
                    graph[src][src] = 0
                elif (src, dst) in link_list:
                    graph[src][dst] = link_list[(src, dst)][2]
        return graph

    def get_path(self, graph, src, flags):
        function = None
        if flags == oxproto_common.OXP_ADVANCED_HOP:
            function = 'full_dijkstra'
        elif flags == oxproto_common.OXP_SIMPLE_HOP:
            function = 'floyd_dict'
        elif flags == oxproto_common.OXP_ADVANCED_BW:
            function = None
        elif flags == oxproto_common.OXP_SIMPLE_BW:
            function = None
        else:
            self.logger.info("No routing algorithm for this model.")
            return None

        result = get_paths(graph, function, src, self.topology)
        if result:
            self.capabilities = result[0]
            self.paths = result[1]
            return self.paths

        self.logger.debug("Path is not found.")
        return None

    @set_ev_cls(oxp_event.EventOXPLinkDiscovery,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def get_topology(self, ev):
        self.graph = self.get_graph(self.topology.links, self.domains)

    def arp_forwarding(self, domain, msg, arp_dst_ip):
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        domain_id = self.get_host_location(arp_dst_ip)

        if domain_id:
            # build packet_out pkt and put it into sbp, send to domain
            domain = self.domains[domain_id]

            out = utils._build_packet_out(
                datapath, ofproto.OFP_NO_BUFFER,
                ofproto.OFPP_CONTROLLER, ofproto.OFPP_LOCAL, msg.data)
            out.serialize()

            sbp_pkt = domain.oxproto_parser.OXPSBP(domain, data=out.buf)
            domain.send_msg(sbp_pkt)
        else:   # access info is not existed. send to all UNknow access port
            for domain in self.domains.values():
                out = utils._build_packet_out(
                    datapath, ofproto.OFP_NO_BUFFER,
                    ofproto.OFPP_CONTROLLER, ofproto.OFPP_LOCAL, msg.data)
                out.serialize()

                sbp_pkt = domain.oxproto_parser.OXPSBP(domain, data=out.buf)
                domain.send_msg(sbp_pkt)

    def shortest_forwarding(self, domain, msg, eth_type, ip_src, ip_dst):
        src_domain = dst_domain = None
        src_domain = self.get_host_location(ip_src)
        dst_domain = self.get_host_location(ip_dst)
        self.get_path(self.graph, src_domain, oxproto_common.OXP_ADVANCED_HOP)

        if self.paths:
            if dst_domain:
                path = self.paths[src_domain][dst_domain]
                self.logger.debug("Path[%s-->%s]:%s" % (ip_src, ip_dst, path))

                access_table = {}
                for domain_id in self.location.locations:
                    access_table[(domain_id, ofproto_v1_3.OFPP_LOCAL
                                  )] = self.location.locations[domain_id]

                flow_info = (eth_type, ip_src, ip_dst, msg.match['in_port'])
                utils.oxp_install_flow(self.domains, self.topology.links,
                                       access_table, path, flow_info, msg)
        else:
            # Reflesh the topology database.
            self.get_topology(None)

    @set_ev_cls(oxp_event.EventOXPSBPPacketIn, MAIN_DISPATCHER)
    def _sbp_packet_in_handler(self, ev):
        msg = ev.msg
        in_port = msg.match['in_port']
        domain = ev.domain
        data = msg.data

        pkt = packet.Packet(msg.data)
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        eth_type = pkt.get_protocols(ethernet.ethernet)[0].ethertype

        # We implemente oxp in a big network,
        # so we shouldn't care about the subnet and router.
        if isinstance(arp_pkt, arp.arp):
            self.arp_forwarding(domain, msg, arp_pkt.dst_ip)

        if isinstance(ip_pkt, ipv4.ipv4):
            self.shortest_forwarding(domain, msg, eth_type,
                                     ip_pkt.src, ip_pkt.dst)
