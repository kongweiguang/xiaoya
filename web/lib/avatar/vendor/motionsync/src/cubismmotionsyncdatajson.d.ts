/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { CubismJson } from '@framework/utils/cubismjson';
import { CubismMotionSyncDataAudioParameter, CubismMotionSyncDataCubismParameter, CubismMotionSyncDataMapping, CubismMotionSyncDataMappingTarget, CubismMotionSyncDataMeta, CubismMotionSyncDataMetaDictionary, CubismMotionSyncDataSetting } from './cubismmotionsyncdata';
/**
 * motionsync3.jsonのコンテナ。
 */
export declare class CubismMotionSyncDataJson {
    /**
     * コンストラクタ
     * @param buffer motionsync3.jsonが読み込まれているバッファ
     * @param size バッファのサイズ
     */
    constructor(buffer: ArrayBuffer, size: number);
    /**
     * デストラクタ相当の処理
     */
    release(): void;
    /**
     * バージョン情報を取得する
     * @return バージョン数
     */
    getVersion(): number;
    /**
     * モーションシンク設定のメタ情報を取得する
     * @return ーションシンク設定のメタ情報
     */
    getMeta(): CubismMotionSyncDataMeta;
    /**
     * Metaのモーションシンク設定リストのアイテムを取得する
     * @param index アイテムのインデックス
     * @return モーションシンク設定リストのアイテム
     */
    getMetaDictionaryItem(index: number): CubismMotionSyncDataMetaDictionary;
    /**
     * モーションシンク設定の数を取得する
     * @return モーションシンク設定の数
     */
    getSettingCount(): number;
    /**
     * Metaからモーションシンク設定のIdを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @return モーションシンク設定のId
     */
    getIdFromMeta(settingIndex: number): string;
    /**
     * モーションシンク設定の名前を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @return モーションシンク設定の名前
     */
    getName(settingIndex: number): string;
    /**
     * Settingsからモーションシンク設定を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @return モーションシンク設定
     */
    getSetting(settingIndex: number): CubismMotionSyncDataSetting;
    /**
     * Settingsからモーションシンク設定のIdを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @return モーションシンク設定のId
     */
    getIdFromSettings(settingIndex: number): string;
    /**
     * モーションシンク設定の音声解析タイプを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @return 音声解析タイプ
     */
    getAnalysisType(settingIndex: number): string;
    /**
     * モーションシンク設定のユースケースを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @return ユースケース
     */
    getUseCase(settingIndex: number): string;
    /**
     * CubismParametarsに登録されているCubismParametarアイテムを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex CubismParametarsから取得したい要素のインデックス
     * @return CubismParametarアイテム
     */
    getCubismParametarsElement(settingIndex: number, elementIndex: number): CubismMotionSyncDataCubismParameter;
    /**
     * CubismParametarsに登録されているCubismParametarの名称を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex CubismParametarsから取得したい要素のインデックス
     * @return CubismParametarの名称
     */
    getNameFromCubismParameters(settingIndex: number, elementIndex: number): string;
    /**
     * CubismParametarsに登録されているCubismParametarのIdを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex CubismParametarsから取得したい要素のインデックス
     * @return CubismParametarのId
     */
    getIdFromCubismParameters(settingIndex: number, elementIndex: number): string;
    /**
     * CubismParametarsに登録されているCubismParametarの最小値を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex CubismParametarsから取得したい要素のインデックス
     * @return CubismParametarの最小値
     */
    getMinFromCubismParameters(settingIndex: number, elementIndex: number): number;
    /**
     * CubismParametarsに登録されているCubismParametarの最大値を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex CubismParametarsから取得したい要素のインデックス
     * @return CubismParametarの最大値
     */
    getMaxFromCubismParameters(settingIndex: number, elementIndex: number): number;
    /**
     * CubismParametarsに登録されているCubismParametarの減衰値を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex CubismParametarsから取得したい要素のインデックス
     * @return CubismParametarの減衰値
     */
    getDamperFromCubismParameters(settingIndex: number, elementIndex: number): number;
    /**
     * CubismParametarsに登録されているCubismParametarのスムージング値を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex CubismParametarsから取得したい要素のインデックス
     * @return CubismParametarのスムージング値
     */
    getSmoothFromCubismParameters(settingIndex: number, elementIndex: number): number;
    /**
     * AudioParametersに登録されている音声の要素を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex AudioParametersから取得したい要素のインデックス
     * @return 音声の要素
     */
    getAudioParametersElement(settingIndex: number, elementIndex: number): CubismMotionSyncDataAudioParameter;
    /**
     * AudioParametersに登録されている音声の要素の名称を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex AudioParametersから取得したい要素のインデックス
     * @return 音声の要素の名称
     */
    getNameFromAudioParameters(settingIndex: number, elementIndex: number): string;
    /**
     * AudioParametersに登録されている音声の要素のIdを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex AudioParametersから取得したい要素のインデックス
     * @return 音声の要素のId
     */
    getIdFromAudioParameters(settingIndex: number, elementIndex: number): string;
    /**
     * AudioParametersに登録されている音声の要素の最小値を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex AudioParametersから取得したい要素のインデックス
     * @return 音声の要素の最小値
     */
    getMinFromAudioParameters(settingIndex: number, elementIndex: number): number;
    /**
     * AudioParametersに登録されている音声の要素の最大値を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex AudioParametersから取得したい要素のインデックス
     * @return 音声の要素の最大値
     */
    getMaxFromAudioParameters(settingIndex: number, elementIndex: number): number;
    /**
     * AudioParametersに登録されている音声の要素のスケール値を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex AudioParametersから取得したい要素のインデックス
     * @return 音声の要素のスケール値
     */
    getScaleFromAudioParameters(settingIndex: number, elementIndex: number): number;
    /**
     * AudioParametersに登録されている音声の要素の有効フラグを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex AudioParametersから取得したい要素のインデックス
     * @return 音声の要素の有効フラグ
     */
    getEnabledFromAudioParameters(settingIndex: number, elementIndex: number): boolean;
    /**
     * Mappingsに登録されているマッピングデータを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex Mappingsから取得したい要素のインデックス
     * @return マッピングデータ
     */
    getMappingsElement(settingIndex: number, elementIndex: number, targetCount: number): CubismMotionSyncDataMapping;
    /**
     * Mappingsに登録されているTargetsの要素を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param mappingIndex Mappingsから取得したい要素のインデックス
     * @param TargetsIndex Targetsから取得したい要素のインデックス
     * @return Targetsの要素
     */
    getMappingTargetsElement(settingIndex: number, mappingIndex: number, targetIndex: number): CubismMotionSyncDataMappingTarget;
    /**
     * マッピングのタイプを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex Mappingsから取得したい要素のインデックス
     * @return マッピングのタイプ
     */
    getMappingType(settingIndex: number, elementIndex: number): string;
    /**
     * Mappingsに登録されている音声の要素のIdを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param elementIndex Mappingsから取得したい要素のインデックス
     * @return 音声の要素のId
     */
    getAudioParamIdFromMappings(settingIndex: number, elementIndex: number): string;
    /**
     * Targetsに登録されているCubismParameterのIdを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param mappingIndex Mappingsから取得したい要素のインデックス
     * @param TargetsIndex Targetsから取得したい要素のインデックス
     * @return CubismParameterのId
     */
    getCubismIdFromMappingTarget(settingIndex: number, mappingIndex: number, targetIndex: number): string;
    /**
     * Targetsに登録されているCubismParameterの値を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @param mappingIndex Mappingsから取得したい要素のインデックス
     * @param TargetsIndex Targetsから取得したい要素のインデックス
     * @return CubismParameterの値
     */
    getValueFromMappingTarget(settingIndex: number, mappingIndex: number, targetIndex: number): number;
    /**
     * ブレンド率を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @return ブレンド率
     */
    getBlendRatio(settingIndex: number): number;
    /**
     * スムージング値を取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @return スムージング値
     */
    getSmoothingFromPostProcessing(settingIndex: number): number;
    /**
     * 設定フレームレートを取得する
     * @param settingIndex モーションシンク設定のインデックス
     * @return 設定フレームレート
     */
    getSampleRate(settingIndex: number): number;
    _json: CubismJson;
}
import * as $ from './cubismmotionsyncdatajson';
export declare namespace Live2DCubismMotionSyncFramework {
    const CubismMotionSyncDataJson: typeof $.CubismMotionSyncDataJson;
    type CubismMotionSyncDataJson = $.CubismMotionSyncDataJson;
}
