import time
import torch
from PIL import Image
from transformers import (
    AutoProcessor,
    Qwen2_5_VLForConditionalGeneration,
    BitsAndBytesConfig,
)


class PersonAnalyzer:
    def __init__(
        self,
        model_id="Qwen/Qwen2.5-VL-3B-Instruct",
        min_pixels=128 * 28 * 28,
        max_pixels=384 * 384,
        use_4bit=True,
    ):
        self.model_id = model_id

        print("모델 로딩 중...")
        start = time.time()

        self.processor = AutoProcessor.from_pretrained(
            self.model_id,
            min_pixels=min_pixels,
            max_pixels=max_pixels,
        )

        if use_4bit:
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
            )

            self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                self.model_id,
                quantization_config=quantization_config,
                device_map="auto",
            )
        else:
            self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                self.model_id,
                torch_dtype=torch.float16,
                device_map="auto",
            )

        end = time.time()
        print("모델 로딩 완료")
        print(f"모델 로딩 시간: {end - start:.2f}초")
        if hasattr(self.model, "hf_device_map"):
            print(self.model.hf_device_map)
        else:
            print("hf_device_map 없음")
    def _build_messages(self, image):
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {
                        "type": "text",
                        "text": """
                          너는 지능형 CCTV 프로그램이야.
                          이미지 속 인물의 성별, 추정 나이대, 상의, 하의, 모자만 추출해.

                          규칙:
                          - 한국어로만 출력
                          - 문장으로 설명하지 말 것
                          - '~입고 있음', '~쓰고 있음', '~입니다' 같은 서술어 금지
                          - 각 항목은 명사형으로만 출력
                          - 의상은 색상을 포함해서 출력
                          - 모자가 보이지 않으면 '모자: 없음' 출력

                          출력 형식은 반드시 아래 형식만 사용:
                          성별:
                          나이대:
                          상의:
                          하의:
                          모자:
                          """,
                    },
                ],
            }
        ]

    def analyze(self, image_or_path, max_new_tokens=50, show_time=True):
        if isinstance(image_or_path, str):
            image = Image.open(image_or_path).convert("RGB")
        else:
            image = image_or_path.convert("RGB")

        messages = self._build_messages(image)

        inputs = self.processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        ).to(self.model.device)

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        if show_time:
            print("이미지 분석 중...")

        infer_start = time.time()

        with torch.no_grad():
            generated_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
            )

        infer_end = time.time()

        generated_ids_trimmed = generated_ids[:, inputs["input_ids"].shape[1]:]

        result = self.processor.batch_decode(
            generated_ids_trimmed,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]

        result = self._clean_result(result)

        if show_time:
            print(f"이미지 분석 시간: {infer_end - infer_start:.2f}초")

        return result

    def _clean_result(self, text):
        text = text.strip()
        text = text.replace("입니다", "")
        text = text.replace("를", "")
        text = text.replace("을", "")
        text = text.replace("입고 있습니다", "")
        text = text.replace("쓰고 있습니다", "")
        text = text.replace(".", "")
        return text


if __name__ == "__main__":
    analyzer = PersonAnalyzer()

    result = analyzer.analyze("person.jpg")

    print("\n===== 분석 결과 =====")
    print(result)