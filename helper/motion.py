import cv2

def motion_score(frame1_path, frame2_path):
    """
    Compare two frames and return a motion score.
    Higher score = larger visual difference.
    """

    img1 = cv2.imread(frame1_path)
    img2 = cv2.imread(frame2_path)

    if img1 is None or img2 is None:
        return 0.0

    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)

    diff = cv2.absdiff(gray1, gray2)

    return float(diff.mean())


def frame_quality(frame_path):
    """
    Return (sharpness, brightness) for a single frame.

    sharpness  = variance of the Laplacian. Low on frozen/loading/blurred
                 frames, high on crisp action. Cheap and model-free.
    brightness = mean luma 0-255. Near 0 = black/loading screen.
    """
    img = cv2.imread(frame_path)
    if img is None:
        return 0.0, 0.0

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    return sharpness, brightness

