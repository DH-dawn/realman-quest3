# Third-party notices

- XRoboToolkit PC Service / native SDK: https://github.com/XR-Robotics/XRoboToolkit-PC-Service
  Pinned Ubuntu 22.04 amd64 release v1.0.0; Apache-2.0 notice retained in licenses/XRoboToolkit_PC_Service_LICENSE.txt.
  Native installers are separate resources; installation instructions are in docs/native_runtime.md.
- Quest client: https://github.com/XR-Robotics/XRoboToolkit-Unity-Client-Quest
  Reference commit 07a28333341629008187e988b73b48bced316092. Original MIT notice retained in licenses/XRoboToolkit_Quest_MIT.txt.
  Compatibility/tracking patches and build reference are in third_party/quest_client/.
- RealMan offline SDK: Robotic_Arm, installed from its distribution. RealSense: pyrealsense2.
  NumPy, SciPy, OpenCV, Pillow and websockets retain their own distribution licenses.
- ARX teleoperation reference: https://github.com/GZF123/arx-televr
  Commit cd557afbb9ec280df2d9e12e1582219b85ee1a53. The RM65 adapter is an independent implementation;
  source/reference details retained in docs/follow_reference.json.
- openpi and its pinned LeRobot dependency are required separately on the training/inference server.

Third-party components retain their respective licenses.
Unity tooling, signing keys and model weights must be prepared separately.
