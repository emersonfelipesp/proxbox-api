"""
Direct test of VM stream to verify the fix is working.
This bypasses auth to test the core streaming logic.
"""
import asyncio
import sys
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import create_virtual_machines
from proxbox_api.utils.streaming import WebSocketSSEBridge
from proxbox_api.dependencies.netbox_client import get_netbox_service
from proxbox_api.dependencies.proxmox_client import get_proxmox_service

async def test_vm_stream():
    print("🔧 Testing VM stream with fix...")
    
    # Create a bridge to capture events
    bridge = WebSocketSSEBridge()
    events_received = []
    
    async def consume_events():
        """Consumer task that collects events"""
        try:
            print("📡 Starting event consumer...")
            async for frame in bridge.iter_sse():
                events_received.append(frame)
                print(f"✅ Received event: {frame[:100]}...")
                if len(events_received) >= 3:  # Get at least 3 events
                    print("✅ Received enough events, stopping consumer")
                    break
        except Exception as e:
            print(f"❌ Consumer error: {e}")
    
    async def produce_events():
        """Producer task that runs the sync"""
        try:
            print("🚀 Starting VM sync...")
            # Note: This will fail due to missing dependencies, but should emit discovery event first
            result = await create_virtual_machines(
                netbox_service=None,  # Will fail, but after emitting discovery
                proxmox_service=None,
                proxmox_endpoint_ids=[],
                netbox_endpoint_ids=[],
                use_guest_agent_interface_name=True,
                fetch_max_concurrency=8,
                ignore_ipv6_link_local_addresses=True,
                websocket=bridge,
                use_websocket=True,
                sync_vm_network=False,
            )
        except Exception as e:
            print(f"⚠️ Producer ended with error (expected): {type(e).__name__}: {e}")
        finally:
            print("🔒 Closing bridge...")
            await bridge.close()
    
    # Run both tasks concurrently
    consumer_task = asyncio.create_task(consume_events())
    producer_task = asyncio.create_task(produce_events())
    
    # Wait with timeout
    try:
        await asyncio.wait_for(
            asyncio.gather(consumer_task, producer_task, return_exceptions=True),
            timeout=5.0
        )
    except asyncio.TimeoutError:
        print("❌ TIMEOUT: No events received within 5 seconds")
        print("❌ This means the fix is NOT working - discovery event never emitted")
        return False
    
    if events_received:
        print(f"\n✅ SUCCESS: Received {len(events_received)} events")
        print("✅ Fix is working - discovery event emitted immediately")
        return True
    else:
        print("\n❌ FAILURE: No events received")
        print("❌ Fix is NOT working")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_vm_stream())
    sys.exit(0 if success else 1)
