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

