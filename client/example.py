"""Minimal lap run from a contestant's own IDE."""

from squadrone import Drone

with Drone() as drone:
    drone.takeoff()
    drone.fly(fb=40, seconds=0.5)
    frame = drone.frame()
    print("camera:", None if frame is None else frame.shape)
    drone.land()
