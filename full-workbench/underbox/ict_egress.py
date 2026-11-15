"""Keep public-source reads out of private/metadata networks, including redirects."""
import ipaddress,socket

def allowed_ip(host,port):
    try:
        address=ipaddress.ip_address(host.split('%')[0])
        if str(address)=='127.0.0.1' and int(port)==8080:return True  # existing local SearXNG
        return int(port) in (80,443) and address.is_global and not (getattr(address,'ipv4_mapped',None) and not address.ipv4_mapped.is_global)
    except (ValueError,TypeError):return False

def install():
    if getattr(socket,'_underbox_ict_guard',False):return
    resolver=socket.getaddrinfo;connect=socket.socket.connect
    def resolve(host,port,*args,**kwargs):
        results=resolver(host,port,*args,**kwargs)
        if any(not allowed_ip(x[4][0],x[4][1]) for x in results):raise OSError('network_destination_blocked')
        return results
    def guarded_connect(sock,address):
        if isinstance(address,tuple) and not allowed_ip(address[0],address[1]):raise OSError('network_destination_blocked')
        return connect(sock,address)
    socket.getaddrinfo=resolve;socket.socket.connect=guarded_connect;socket._underbox_ict_guard=True
