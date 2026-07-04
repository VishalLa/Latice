from pathlib import Path
from paddleocr import PaddleOCR
from typing import Optional


class Block:
    """
    One detected word returned by PaddleOCR.

    Attributes
    ----------
    x, y : top-left pixel coordinates of the bounding box
    text : recognised text
    conf : recognition confidence in [0, 1]
    """

    def __init__(self, x: int, y: int, text: str, conf: float) -> None:
        self.x    = x
        self.y    = y
        self.text = text
        self.conf = conf

    def __repr__(self) -> str:
        return f"Block(y={self.y}, x={self.x}, text={self.text!r})"
    

class OCR: 
    """
    initlize paddleOCR and detect char
    """
    def __init__(self):
        self.ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False, use_gpu=False)


    @staticmethod
    def ocr_to_blocks(ocr_result, conf_threshold=0.25) -> list[Block]:
        """
        Convert raw PaddleOCR result to sorted Block list.
        ocr_result: output form paddleocr
        """
        blocks = []
        if not ocr_result or not ocr_result[0]:
            return blocks
        
        for line in ocr_result[0]:
            try:
                box, (text, conf) = line 

                if conf >= conf_threshold and text.strip():
                    x = int(box[0][0])
                    y = int(box[0][1])
                    blocks.append(Block(x, y, text.strip(), conf))

            except Exception:
                continue
        blocks.sort(key=lambda b:(b.y, b.x))
        return blocks
    

    def ocr_image(self, image_path: Path) -> list[Block]:
        result = self.ocr.ocr(str(image_path), cls=True)
        return self.ocr_to_blocks(result)
    

# get singleton object of OCR
ocr_instance: Optional[OCR] = None 

def get_ocr() -> OCR: 
    global ocr_instance 
    if ocr_instance is None:
        ocr_instance = OCR()
    return ocr_instance
