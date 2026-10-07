import * as $ from './cubismmotionsyncengineanalysisresult';
export declare class CubismMotionSyncEngineAnalysisResult {
    constructor(valuesSize: number);
    copy(result: CubismMotionSyncEngineAnalysisResult): void;
    /** 结果结构固定为三个字段，与输出参数数量无关，避免两参数嘴型发生原生内存越界。 */
    toNativeArray(forceConversion: boolean): Int32Array;
    /** 初始化失败或重复释放时也必须安全，不能读取尚未分配的结果数组。 */
    releaseNativeArray(): void;
    /** 显式回收输出数组的原生内存，不能只丢弃 JavaScript 值数组。 */
    release(): void;
    getValues(): Array<number>;
    getValuesCount(): number;
    getProcessedSampleCount(): number;
    setProcessedSampleCount(sampleCount: number): void;
    ConvertNativeAnalysisResult(nativeArrayPtr: number): void;
    private ConvertFromNativeResultValues;
    private ConvertFromNativeProcessedSampleCount;
    private deallocNativeArrayPtr;
    private _values;
    private _valuesCount;
    private _processedSampleCount;
    private _resultArray;
    private _resultArrayPtr;
}
export declare namespace Live2DCubismMotionSyncFramework {
    const CubismMotionSyncEngineAnalysisResult: typeof $.CubismMotionSyncEngineAnalysisResult;
    type CubismMotionSyncEngineAnalysisResult = $.CubismMotionSyncEngineAnalysisResult;
}
