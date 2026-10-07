// Namespace definition for compatibility.
import * as $ from './cubismmotionsyncengineanalysisresult';

/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

import ToPointer = Live2DCubismMotionSyncCore.ToPointer;

const s_analysisResultStructSize = 3;

export class CubismMotionSyncEngineAnalysisResult {
  public constructor(valuesSize: number) {
    this._values = new Array<number>(valuesSize);
    this._valuesCount = valuesSize;
    this._processedSampleCount = 0;
  }

  public copy(result: CubismMotionSyncEngineAnalysisResult): void {
    this._values = new Array<number>();

    for (let index = 0; index < result.getValues().length; index++) {
      this._values.push(result.getValues()[index]);
    }

    this._valuesCount = result.getValuesCount();
    this._processedSampleCount = 0;
  }

  /** 结果结构固定为三个字段，与输出参数数量无关，避免两参数嘴型发生原生内存越界。 */
  public toNativeArray(forceConversion: boolean): Int32Array {
    // 強制的に新規作成しないのであれば既にあるものを返す
    if (!forceConversion && this._resultArray) {
      return this._resultArray;
    }

    if (this._resultArray) {
      this.releaseNativeArray();
    }
    this._resultArray = new Int32Array(s_analysisResultStructSize);
    this._resultArrayPtr = ToPointer.Malloc(
      this._resultArray.length * this._resultArray.BYTES_PER_ELEMENT
    );

    // Nativeポインタへの変換
    this._resultArray = ToPointer.ConvertAnalysisResultToInt32Array(
      this._resultArray,
      this._resultArrayPtr,
      this._valuesCount
    );

    return this._resultArray;
  }

  /** 初始化失败或重复释放时也必须安全，不能读取尚未分配的结果数组。 */
  public releaseNativeArray(): void {
    if (!this._resultArray) return;
    this.deallocNativeArrayPtr();
    this._resultArray = void 0;
  }

  /** 显式回收输出数组的原生内存，不能只丢弃 JavaScript 值数组。 */
  public release(): void {
    this.releaseNativeArray();
    this._values = void 0;
    this._values = null;
    this._valuesCount = 0;
    this._processedSampleCount = 0;
  }

  public getValues(): Array<number> {
    return this._values;
  }

  public getValuesCount(): number {
    return this._valuesCount;
  }

  public getProcessedSampleCount(): number {
    return this._processedSampleCount;
  }

  public setProcessedSampleCount(sampleCount: number): void {
    this._processedSampleCount = sampleCount;
  }

  public ConvertNativeAnalysisResult(nativeArrayPtr: number): void {
    this.ConvertFromNativeResultValues();
    this.ConvertFromNativeProcessedSampleCount(nativeArrayPtr);
  }

  private ConvertFromNativeResultValues(): void {
    this._values = ToPointer.GetValuesFromAnalysisResult(this._resultArray[0], this._valuesCount);
  }

  private ConvertFromNativeProcessedSampleCount(nativeArrayPtr: number): void {
    this._processedSampleCount = ToPointer.GetProcessedSampleCountFromAnalysisResult(
      nativeArrayPtr + 8
    );
  }

  private deallocNativeArrayPtr(): void {
    // 参照渡しになっている箇所だけ先にメモリ解放
    ToPointer.Free(this._resultArray[0]);

    // 配列本体を解放
    ToPointer.Free(this._resultArrayPtr);
    this._resultArrayPtr = 0;
  }

  private _values: Array<number>;
  private _valuesCount: number;
  private _processedSampleCount: number;
  private _resultArray: Int32Array;
  private _resultArrayPtr: number;
}

// eslint-disable-next-line @typescript-eslint/no-namespace
export namespace Live2DCubismMotionSyncFramework {
  export const CubismMotionSyncEngineAnalysisResult = $.CubismMotionSyncEngineAnalysisResult;
  export type CubismMotionSyncEngineAnalysisResult = $.CubismMotionSyncEngineAnalysisResult;
}
