Hailo model files for the Raspberry Pi 5 + AI HAT+ 2 (config.yaml):
  yolo26s.hef   detection loop (required)
  yolo26n.hef   door counter (optional; without it the door uses yolo26s.hef)
  labels.txt    only for a custom-trained model (model: labels: models/labels.txt)
How to make them: tools/hailo_compile/README.md. Check them: tools/hailo_check.py.
